"""Durable attempts, remote reconciliation and separate Judge retries."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .core import Failure, digest

TERMINAL={'completed','failed','cancelled','awaiting_input'}
SETTLED={'settled','execution_error','cancelled','manual'}
JUDGE_SYSTEM='''你是评测判分员，比较问题、参考答案和智能体回答。
pass=关键结论与数值一致；partial=方向正确但遗漏；fail=关键结论错误。
先对齐比较范围：整体、月度或汇总答案只和同层级汇总比较，不能拿单行局部值替代。
组合或方案编号通常是用例标识，不是批次。百分比绝对误差不超过 0.05 个百分点，
或相对误差不超过 0.1%，任一满足即视为舍入差异；一般数值相对误差不超过 0.1% 可视为一致。
不因尾随零、格式或不矛盾的额外说明降分。用 submit_verdict 提交结论和简短理由。'''


class Judge:
    def __init__(self,config,http): self.config,self.http=config,http

    def profile(self):
        c=self.config
        if not all((c.judge_url,c.judge_key,c.judge_model,c.judge_revision)):
            raise Failure('judge_unconfigured','Judge 配置缺失（MODEL-CONFIG-01）',503,'judging')
        return dict(model=c.judge_model,revision=c.judge_revision,rule_version='1')

    def score(self, question, reference, answer):
        profile=self.profile()
        schema={'type':'object','properties':{'verdict':{'type':'string','enum':['pass','partial','fail']},
                 'reason':{'type':'string'}},'required':['verdict','reason'],'additionalProperties':False}
        response=self.http.json('POST',self.config.judge_url,'/chat/completions',token=self.config.judge_key,
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

    def preflight(self):
        self.score('合成协议检查：1 加 1 等于几？','2','2')


class Consumer:
    def __init__(self,config,http): self.config,self.http=config,http

    def call(self,method,path,token,workspace='',**kwargs):
        return self.http.json(method,self.config.consumer_url,'/agentic'+path,token=token,workspace=workspace,**kwargs)

    def ensure(self,token,run_id):
        if not self.config.consumer_token:
            raise Failure('consumer_unconfigured','请配置受限 evaluation 消费凭据',503)
        return self.call('POST','/internal/evaluation/workspaces/ensure',token,
                         headers={'X-Evaluation-Service-Token':self.config.consumer_token},json={'run_id':run_id})['workspace']['id']

    def profile(self,token,ws):
        value=self.call('GET','/model-profiles',token,ws)
        selected=value.get('default_profile_id')
        if not selected:
            raise Failure('model_unconfigured','consumer 默认问答模型未配置',503,'preparing')
        return dict(id=selected,revision=digest(value),profiles=value)

    def install(self,token,ws,raw,receipt):
        try:
            self.call('POST','/ontologies/import-zip',token,ws,files={'file':('package.zip',raw,'application/zip')})
        except Failure as exc:
            if exc.status!=409: raise
            # Only this run owns this space. Retry the exact immutable Hub bytes.
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
            return self.store.save(owner,rid,run)

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
        if run['judge']!=self.judge.profile():
            raise Failure('configuration_changed','Judge 配置已变化，请创建新轮次',409)
        self.judge.preflight()
        if not run.get('workspace'):
            ws=self.consumer.ensure(token,rid)
            run=self.update(owner,rid,workspace=ws,managed_workspace=True,status='preparing')
        ws=run['workspace']
        profile=self.consumer.profile(token,ws)
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
            source=self.data.prepare(rid,run['source'],run.get('mapping'),run.get('relations'))
            if source:
                self.consumer.call('PUT','/ontologies/'+quote(run['package_ref']['manifest_id'],safe='')+'/data-source',token,ws,json=source)
                probe=self.consumer.call('POST','/ontologies/'+quote(run['package_ref']['manifest_id'],safe='')+'/data-source/test',token,ws,json={})
                if probe.get('success') is not True:
                    raise Failure('data_source_unavailable','消费数据源连接检查失败',503,'data_import')
            run=self.update(owner,rid,data_ready=True)
        self.update(owner,rid,status='running',phase='cases')
        for index in range(len(run['cases'])):
            if self.stopping: return
            run=self.store.get(owner,rid,'runs')
            case=run['cases'][index]
            token=self.token(owner,rid)
            cancelling=run.get('cancel_requested',False)
            if case['status'] in SETTLED: continue
            if cancelling:
                self._cancel(owner,rid,run)
                return
            if not case.get('answer'):
                if not case.get('remote_id'):
                    # Consumer /runs persists the session fingerprint and idempotency key.
                    # Reconciliation resubmits the exact same request, never a new session.
                    if self.consumer.profile(token,ws)!=run['model']:
                        raise Failure('configuration_changed','问答模型发生变化',409)
                    case['status']='unknown'
                    self.update(owner,rid,cases=run['cases'])
                    response=self.consumer.call('POST','/runs',token,ws,
                        headers={'Idempotency-Key':rid+'-'+str(index)},json={
                            'request':case['question'],'session_id':'eval-'+rid+'-'+str(index),
                            'model_profile_id':run['model']['id'],'include_trace':True})
                    case['remote_id']=response['run_id']
                    case['status']='executing'
                    self.update(owner,rid,cases=run['cases'])
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
                    self.update(owner,rid,cases=run['cases'])
                    continue
                case['answer']=answer
                case['status']='answer_ready'
                self.update(owner,rid,cases=run['cases'])
            if not case.get('reference'):
                case['status']='manual'
            else:
                try:
                    case['judgement']=self.judge.score(case['question'],case['reference'],case['answer'])
                    case['status']='settled'
                except Failure as exc:
                    case['status']='judge_error'
                    case['error']=exc.payload()
            self.update(owner,rid,cases=run['cases'])
        run=self.store.get(owner,rid,'runs')
        self.update(owner,rid,status='cancelled' if run.get('cancel_requested') else (
            'interrupted' if any(c['status']=='judge_error' for c in run['cases']) else 'completed'),phase='finished')

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
            else:
                case['status']='cancelled'
            self.update(owner,rid,cases=run['cases'])
        self.update(owner,rid,status='cancelled',phase='finished')
