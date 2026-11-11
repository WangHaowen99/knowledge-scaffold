"""Durable attempts, remote reconciliation and separate Judge retries."""
import base64
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from . import solver_validation
from .artifacts import clip, failed_first, redact
from .core import Failure, digest, transient
from .judge_settings import connection_test_failure

TERMINAL={'completed','failed','cancelled','awaiting_input'}
SETTLED={'settled','execution_error','cancelled','manual'}
# Workspace IDs consumer issues for evaluation runs.
EVAL_WORKSPACE=re.compile(r'eval-[0-9a-f]{32}')
# Questions of one run sent to consumer at the same time; chosen per run when it is created.
DEFAULT_CONCURRENCY=4
MAX_CONCURRENCY=16
# A consumer request of one case that hits a rate limit, an unavailable service or a timeout is sent again after
# Retry-After, or 5s doubling up to 60s, at most this many times in all; the same failure in this many cases in a
# row stops the run instead of letting every remaining case wait out its retries.
REMOTE_ATTEMPTS=6
REMOTE_DELAY=5
REMOTE_MAX_DELAY=60
BREAKER_CASES=3
# A question still running on consumer's side after this long is left for reconciliation instead of waited for.
REMOTE_SECONDS=900
# Failures that concern the whole run rather than one question: it waits for the user instead of going on.
RUN_FAILURES={'configuration_changed','identity_changed','login_expired','draining','model_unavailable','model_unconfigured'}
# Answering models a run may choose: the consumer global pool and the user's own personal pool. Workspace models are
# left out because the listing workspace is not the workspace the run executes in.
SELECTABLE_MODEL_SCOPES=('global','user')
# Tool failures kept per case; each message is at most 1 KiB, as in the consumer's own traces.
TRACE_FAILURES=20
# Tool calls kept per case as evidence for optimization suggestions: at most 12 calls, each preview
# 200 characters after redaction, so one case stays near 10k characters.
EVIDENCE_CALLS=12
EVIDENCE_PREVIEW=200
# A run's login is checked with the account service at most this often, and renewed from its refresh
# token this long before the access token expires (access tokens live 15 minutes by default).
VERIFY_SECONDS=60
RENEW_SECONDS=120
LOGIN_EXPIRED='登录凭证已过期，请重新登录后点击“授权并继续”'
# Consumer fails a run with SESSION_PREPARE_FAILED when it cannot set up the session before the question starts, e.g.
# when concurrent cases in one workspace collide with session compaction. Nothing was answered, so the case is resubmitted
# in a new session, at most this many submissions in all, as the earlier evaluation pipeline did.
SESSION_ATTEMPTS=6
SESSION_PREPARE_FAILED='会话运行状态准备失败'
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


def preview(value):
    """One bounded, redacted line of a tool input, output or error preview."""
    if value is None or value=='': return ''
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    return clip(' '.join(redact(text).split()),EVIDENCE_PREVIEW)


def error_text(error):
    return str((error.get('message') or '') if isinstance(error,dict) else (error or '')).strip()


def selectable_model(item):
    """Whether a consumer model profile is one a run may choose as its answering model."""
    return isinstance(item,dict) and item.get('scope') in SELECTABLE_MODEL_SCOPES and isinstance(item.get('id'),str)


def model_choice(item):
    """The bounded, non-secret fields of a selectable model shown when creating a run."""
    return {'id':item['id'],'name':str(item.get('name') or '')[:200],'model':str(item.get('model') or '')[:200],
            'provider':str(item.get('provider') or '')[:100],'is_default':bool(item.get('is_default')),
            'scope':item['scope']}


class Consumer:
    def __init__(self,config,http): self.config,self.http=config,http

    def call(self,method,path,token,workspace='',**kwargs):
        return self.http.json(method,self.config.consumer_url,f'/agentic{path}',token=token,workspace=workspace,**kwargs)

    def traffic(self):
        """Marks questions and cancellations as evaluation traffic, which consumer does not collect as user feedback.

        Connection sources run in an ordinary workspace the user named, so consumer cannot tell them apart by workspace."""
        return {'X-Evaluation-Service-Token':self.config.consumer_token} if self.config.consumer_token else {}

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
        if not isinstance(workspace_id,str) or not EVAL_WORKSPACE.fullmatch(workspace_id):
            raise Failure('consumer_workspace_protocol','consumer 未返回有效的评测空间标识，请检查服务版本',502,'workspace_prepare')
        return workspace_id

    def scope_workspace(self,token,workspace_id):
        """Use the consumer workspace whose ID the data source names, with a name equal to that ID.

        Only the user's own workspace permissions are used: create it when missing, otherwise reuse it only
        when this user owns it and has not shared it, because the run imports and activates a package there.
        """
        path=f"/workspaces/{quote(workspace_id,safe='')}"
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

    def trace(self,token,ws,remote_id):
        """Tool calls and failures of a finished consumer run, read from its bounded event replay.

        The user token only lives for the run, so this evidence must be collected when a case settles.
        It never fails the case: the report states that the trace was unavailable instead. ``calls`` are
        numbered in call order and stored apart from the case as evidence for optimization suggestions.
        """
        try:
            raw=self.http.request('GET',self.config.consumer_url,f"/agentic/runs/{quote(remote_id,safe='')}/events?after_seq=0",
                                  token=token,workspace=ws,limit=1024*1024,timeout=15)
            calls,failures,items,open_calls=0,[],[],[]
            for block in raw.decode('utf-8').split('\n\n'):
                data='\n'.join(line[5:].strip() for line in block.splitlines() if line.startswith('data:'))
                event=json.loads(data) if data else None
                if not isinstance(event,dict): continue
                payload=event.get('payload') if isinstance(event.get('payload'),dict) else {}
                name=str(payload.get('name') or payload.get('tool_name') or '').strip()[:256]
                key=str(payload.get('tool_call_id') or '')
                if event.get('event_type')=='tool.started':
                    calls+=1
                    item={'n':len(items)+1,'tool':name[:64],'status':'started',
                          'input':preview(payload.get('input_preview',payload.get('input')))}
                    items.append(item)
                    open_calls.append((key,item))
                elif event.get('event_type')=='tool.finished':
                    # Pair by call ID; without one, by the earliest unfinished call of the same tool.
                    match=next((pair for pair in open_calls if (pair[0]==key if key else pair[1]['tool']==name[:64])),None)
                    if match:
                        open_calls.remove(match)
                        item=match[1]
                    else:
                        item={'n':len(items)+1,'tool':name[:64],'status':'started','input':''}
                        items.append(item)
                    failed=str(payload.get('status') or '').strip().lower() in ('error','failed')
                    item['status']='failed' if failed else 'completed'
                    item['output']=preview(payload.get('output_preview'))
                    if failed:
                        error=error_text(payload.get('error'))
                        item['error']=preview(error)
                        item['code']=str(payload.get('error_code') or '')[:80]
                        if name and error and len(failures)<TRACE_FAILURES:
                            failures.append({'tool_name':name,'error':error[:1024]})
            kept=failed_first([{k:v for k,v in item.items() if v!=''} for item in items],EVIDENCE_CALLS)
            return {'status':'complete','tool_calls':calls,'tool_failures':failures,'calls':kept,'omitted_calls':len(items)-len(kept)}
        except Failure as exc:
            return {'status':'unavailable','error':exc.message}
        except ValueError:  # also UnicodeDecodeError
            return {'status':'unavailable','error':'执行事件格式无法解析'}

    def selectable_models(self,token):
        """Enabled models of the consumer global pool and the user's personal pool, at most 100. Listing needs any
        workspace the user can open; personal models belong to the user, not to that workspace."""
        listing=self.call('GET','/workspaces',token)
        ids=[item.get('id') for item in listing.get('workspaces') or [] if isinstance(item,dict)]
        workspace=listing.get('preferred_workspace_id') or (ids[0] if ids else '')
        if not workspace:
            raise Failure('workspace_missing','当前账号在 consumer 中没有可用的工作空间',409)
        value=self.call('GET','/model-profiles',token,workspace)
        return [model_choice(item) for item in value.get('profiles') or [] if selectable_model(item)][:100]

    def profile(self,token,ws,requested=''):
        value=self.call('GET','/model-profiles',token,ws)
        profiles=value.get('profiles') or []
        if requested and not any(selectable_model(item) and item['id']==requested for item in profiles):
            raise Failure('model_unavailable','所选问答模型已不在 consumer 全局模型池或个人模型池，或已停用，请重新选择后创建新轮次',
                          409,'preparing')
        selected=requested or value.get('default_profile_id')
        if not selected:
            raise Failure('model_unconfigured','consumer 默认问答模型未配置',503,'preparing')
        return dict(id=selected,revision=digest(value),profiles=value)

    def install(self,token,ws,raw,receipt):
        """Import and activate the exact Hub bytes; returns consumer's import response, which lists unusable operations."""
        def upload(**extra):
            try:
                return self.call('POST','/ontologies/import-zip',token,ws,files={'file':('package.zip',raw,'application/zip')},**extra)
            except Failure as exc:
                # Overwriting cannot help: consumer keeps one package per name and an ID other than this one holds it.
                if exc.upstream_code=='ontology_name_conflict':
                    raise Failure('package_name_conflict','评测工作空间中已有同名但本体 ID 不同的知识包（常见于知识包身份统一前导入的旧版本），'
                                  '无法安装本次评测的版本。请在消费服务中删除该工作空间里的旧知识包，或在数据源中更换评测工作空间后恢复评测',
                                  409,'package_install') from exc
                raise
        try:
            imported=upload()
        except Failure as exc:
            if exc.status!=409 or exc.code=='package_name_conflict': raise
            # Retry the exact immutable Hub bytes of this version, also in a reused graph workspace.
            imported=upload(data={'action':'overwrite_version'})
        result=self.call('PUT',f"/ontologies/{quote(receipt['ontology_id'],safe='')}/active-version",token,ws,
                         json={'version':receipt['version']})
        if result.get('active_version')!=receipt['version']:
            raise Failure('activation_mismatch','消费激活版本不一致',409,'package_activate')
        return imported

    def events(self,token,ws,remote_id,limit):
        """The replayed public events of one finished consumer run."""
        return self.http.request('GET',self.config.consumer_url,f"/agentic/runs/{quote(remote_id,safe='')}/events?after_seq=0",
                                 token=token,workspace=ws,limit=limit,timeout=15)

    def candidate_count(self,token,ws,job_id):
        """How many candidates an authoritative solver job produced; an unreadable result is reported, not raised."""
        try:
            value=self.http.json('GET',self.config.consumer_url,f"/api/solver/history/{quote(job_id,safe='')}/candidate-results",
                                 token=token,workspace=ws,limit=8*1024*1024,timeout=30)
        except Failure as exc:
            code=exc.upstream_code or f'http_{exc.upstream_status or exc.status}'
            return {'candidate_count':None,'candidate_results_error':code[:120]}
        count=value.get('candidate_count') if isinstance(value,dict) else None
        if isinstance(count,int) and not isinstance(count,bool):
            return {'candidate_count':count}
        return {'candidate_count':None,'candidate_results_error':'candidate_count_missing'}


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


def prepare_failed(remote):
    """Whether consumer failed the run before it started; older consumers report only the message."""
    if remote.get('status')!='failed': return False
    error=remote.get('error')
    if isinstance(error,dict):
        return error.get('error_code')=='SESSION_PREPARE_FAILED' or SESSION_PREPARE_FAILED in str(error.get('message') or '')
    return SESSION_PREPARE_FAILED in str(error or '')


def retry_delay(attempt):
    """Seconds before the given retry: doubling from 2 up to 30, jittered so concurrent cases do not collide again."""
    return min(2*2**(attempt-1),30)*random.uniform(.8,1.2)


def remote_delay(attempt,hint=None):
    """Seconds before resending a consumer request: the dependency's Retry-After, else doubling from 5 up to 60."""
    if hint is not None: return min(hint,REMOTE_MAX_DELAY)
    return min(REMOTE_DELAY*2**(attempt-1),REMOTE_MAX_DELAY)*random.uniform(.8,1.2)


class Paused(Exception):
    """A case stopped waiting because the run is being cancelled or the service stops; it stays as it was."""


class Breaker:
    """Counts cases in a row that failed with the same dependency error; any case that settles resets it."""

    def __init__(self,limit):
        self.limit,self.lock,self.key,self.count=limit,threading.Lock(),None,0

    def failed(self,exc):
        if not transient(exc): return False
        key=(exc.code,exc.upstream_status)
        with self.lock:
            self.count=self.count+1 if key==self.key else 1
            self.key=key
            return self.count>=self.limit

    def passed(self):
        with self.lock:
            self.key,self.count=None,0


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


class Credential:
    """One run's login, only in memory: never stored, logged or returned."""

    def __init__(self,access,refresh=''):
        self.access,self.refresh,self.checked=access,refresh,0.0
        self.lock=threading.Lock()

    def due(self,now):
        """Renew only a token whose expiry is known and near; an opaque token waits for its rejection."""
        exp=expiry(self.access)
        return bool(self.refresh) and 0<exp and exp-now<RENEW_SECONDS


def expiry(token):
    """The exp claim of a JWT access token, read without verification and only to decide when to renew."""
    try:
        part=token.split('.')[1]
        return float(json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4))).get('exp') or 0)
    except (IndexError,ValueError,TypeError,AttributeError):
        return 0.0


def login_failure(exc):
    """Any rejected login becomes one actionable message; other failures stay as they are."""
    return Failure('login_expired',LOGIN_EXPIRED,401,'authorization') if exc.status==401 else exc


class Engine:
    def __init__(self,store,identity,packages,data_sources,consumer,judge):
        self.store,self.identity,self.packages,self.data,self.consumer,self.judge=store,identity,packages,data_sources,consumer,judge
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='evaluation')
        self.guard=threading.RLock()
        self.tokens={}
        self.active=set()
        self.stopping=False
        self.on_stop=None  # Called with (owner, run id, user token) whenever execution of a run ends.

    def start(self,owner,rid,token,refresh=''):
        with self.guard:
            if self.stopping:
                raise Failure('draining','服务正在停止',503)
            previous=self.tokens.get((owner,rid))
            # A client that sends no refresh token keeps the one handed over earlier for this run.
            credential=Credential(token,refresh or (previous.refresh if previous else ''))
            if (owner,rid) not in self.active:
                if len(self.active)>=16 or any(key[0]==owner for key in self.active):
                    raise Failure('evaluation_busy','已有活动评测或服务队列已满，请稍后恢复',409)
                run=self.store.get(owner,rid,'runs')
                # Show the resumed run as working at once, not only after preflight and data checks finish.
                if run['status'] in ('interrupted','waiting_auth') and not run.get('cancel_requested'):
                    self.update(owner,rid,status='preparing',error=None)
                self.tokens[(owner,rid)]=credential
                self.active.add((owner,rid))
                self.pool.submit(self.execute,owner,rid)
            else:
                self.tokens[(owner,rid)]=credential

    def token(self,owner,rid):
        """The run's access token, renewed shortly before it expires and checked at most once a minute."""
        credential=self.tokens.get((owner,rid))
        if not credential:
            raise Failure('login_expired',LOGIN_EXPIRED,401,'authorization')
        with credential.lock:
            now=time.time()
            if credential.due(now):
                self.renew(credential)
            if now-credential.checked>=VERIFY_SECONDS:
                try:
                    subject=self.identity.verify(credential.access)
                except Failure as exc:
                    raise login_failure(exc) from exc
                if subject!=owner:
                    raise Failure('identity_changed','用户身份发生变化',403,'authorization')
                credential.checked=now
            return credential.access

    def token_source(self,credential):
        """A login for work after the run, such as optimization suggestions: renewed shortly before it expires."""
        def access():
            with credential.lock:
                if credential.due(time.time()):
                    self.renew(credential)
                return credential.access
        return access

    def remote(self,owner,rid,call):
        """One consumer request of a case with the run's current token; transient failures are retried with backoff."""
        attempt=0
        while True:
            try:
                return call(self.token(owner,rid))
            except Failure as exc:
                attempt+=1
                if not transient(exc) or attempt>=REMOTE_ATTEMPTS: raise
                if not self.backoff(owner,rid,remote_delay(attempt,exc.retry_after)): raise Paused() from exc

    def renew(self,credential):
        """Best effort: on failure the current access token is used until it expires."""
        try:
            value=self.identity.renew(credential.refresh)
        except Failure as exc:
            if exc.upstream_status in (400,401) or exc.status==401:
                credential.refresh=''  # Revoked or expired: wait for the user to authorize again.
            return
        credential.access=value['access_token']
        credential.refresh=value.get('refresh_token') or credential.refresh
        credential.checked=0.0  # The new token is checked against the owner before use.

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
            exc=login_failure(exc)
            pending_cancel=self.store.get(owner,rid,'runs').get('cancel_requested',False)
            self.update(owner,rid,status='waiting_auth' if exc.status in (401,403) else 'cancelling' if pending_cancel else 'interrupted',error=exc.payload())
        except Exception:
            # No raw dependency exception or connection configuration in reports/logs.
            self.update(owner,rid,status='interrupted',error={'code':'execution_interrupted','message':'执行中断，请检查依赖后恢复'})
        finally:
            with self.guard:
                self.active.discard((owner,rid))
                credential=self.tokens.pop((owner,rid),None)
            # Report and suggestion generation outlive the run; they keep its login and renew it when due.
            if self.on_stop and credential: self.on_stop(owner,rid,self.token_source(credential))

    def _execute(self,owner,rid):
        run=self.store.get(owner,rid,'runs')
        token=self.token(owner,rid)
        if run['status'] in ('completed','cancelled'): return
        if run.get('cancel_requested'):
            self._cancel(owner,rid,run)
            return
        if not run.get('started_at'): run=self.update(owner,rid,started_at=time.time(),error=None)
        solver=run.get('run_type')=='solver'
        # Solver validation is judged by deterministic assertions; only data evaluation needs the Judge.
        if not solver:
            if run['judge']!=self.judge.profile(owner=owner):
                raise Failure('configuration_changed','Judge 配置已变化，请创建新轮次',409)
            try:
                self.judge.preflight(owner=owner)
            except Failure as exc:
                # A model gateway rejecting its API Key must not read as an expired user login.
                if exc.code=='dependency_rejected': raise connection_test_failure(exc) from exc
                raise
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
            imported=self.consumer.install(token,ws,raw,receipt)
            unusable=solver_validation.unavailable(imported) if solver else {}
            run=self.update(owner,rid,installed=True,package_ref=receipt,unavailable_operations=unusable)
        if not run.get('data_ready'):
            self.update(owner,rid,phase='data_import')
            source_definition=dict(run['source'])
            if source_definition.get('provider')=='neo4j' and source_definition.get('connection'):
                graph_config=self.store.decrypt(source_definition['connection'])
                if graph_config.get('ontology_property'): graph_config['ontology_value']=run['package_ref']['ontology_id']
                source_definition['connection']=self.store.encrypt(graph_config)
            source=self.data.prepare(rid,source_definition,run.get('mapping'),run.get('relations'))
            if source:
                self.consumer.call('PUT',f"/ontologies/{quote(run['package_ref']['ontology_id'],safe='')}/data-source",token,ws,json=source)
                probe=self.consumer.call('POST',f"/ontologies/{quote(run['package_ref']['ontology_id'],safe='')}/data-source/test",token,ws,json={})
                if probe.get('success') is not True:
                    raise Failure('data_source_unavailable','消费数据源连接检查失败',503,'data_import')
            run=self.update(owner,rid,data_ready=True)
        if run['source'].get('provider')=='neo4j' and run['source'].get('import_data'):
            self.data.graph.ensure_current(rid,run['source'],run['package_ref']['ontology_id'])
        self.update(owner,rid,status='running',phase='cases',error=None)
        halt=threading.Event()
        breaker=Breaker(BREAKER_CASES)
        def work(index):
            if halt.is_set() or self.stopping: return
            try:
                self._case(owner,rid,ws,index,halt)
            except Paused:
                return
            except Failure as exc:
                if exc.status in (401,403) or exc.code in RUN_FAILURES:
                    halt.set()  # The whole run waits for the user; cases already in flight still settle.
                    raise
                self.case_failed(owner,rid,index,exc)
                if breaker.failed(exc):
                    halt.set()
                    raise Failure('dependency_circuit_open',f'连续 {BREAKER_CASES} 条用例因依赖服务暂不可用而失败，已暂停提交其余用例；'
                                  '依赖服务恢复后点击“恢复”继续，已完成的用例会保留',503,'cases') from exc
                return
            except BaseException:
                halt.set()  # Queued cases are skipped; cases already in flight still settle.
                raise
            breaker.passed()
        limit=solver_validation.MAX_CONCURRENCY if solver else MAX_CONCURRENCY
        width=max(1,min(limit,int(run.get('concurrency') or DEFAULT_CONCURRENCY)))
        with ThreadPoolExecutor(max_workers=width,thread_name_prefix='evaluation-case') as pool:
            futures=[pool.submit(work,index) for index in range(len(run['cases']))]
        for future in futures:
            future.result()
        if self.stopping: return
        run=self.store.get(owner,rid,'runs')
        if run.get('cancel_requested'):
            self._cancel(owner,rid,run)
            return
        unsettled=sum(1 for c in run['cases'] if c['status'] not in SETTLED and c['status']!='judge_error')
        if unsettled:
            # Their questions may still be running on consumer's side, or consumer was unavailable: resume reconciles them.
            self.update(owner,rid,status='interrupted',phase='finished',error={'code':'cases_unsettled','phase':'cases','retryable':True,
                        'message':f'有 {unsettled} 条用例未完成（依赖服务暂不可用或消费仍在执行），请稍后点击“恢复”，只会处理这些用例'})
            return
        self.update(owner,rid,status='interrupted' if any(c['status']=='judge_error' for c in run['cases']) else 'completed',
                    phase='finished')

    def case_failed(self,owner,rid,index,exc):
        """Record why one case did not settle and go on with the others.

        A request consumer refused cannot succeed by asking again, so the case is an execution error. After a rate
        limit, an outage or a time-out the question may still be running remotely: the case keeps its state and the
        reason, and resume reconciles it under the same idempotency key without asking twice."""
        with self.guard:
            run=self.store.get(owner,rid,'runs')
            case=run['cases'][index]
            if case['status'] in SETTLED: return
            case['error']=exc.payload()
            if not transient(exc) and exc.code!='remote_timeout':
                case['status']='execution_error'
            self.save_case(owner,rid,index,case)

    def _case(self,owner,rid,ws,index,halt):
        run=self.store.get(owner,rid,'runs')
        case=run['cases'][index]
        if case['status'] in SETTLED: return
        if run.get('cancel_requested'):
            halt.set()  # Unsettled cases are cancelled once, after every worker has stopped.
            return
        token=self.token(owner,rid)
        solver=run.get('run_type')=='solver'
        unusable=run.get('unavailable_operations') or {}
        blocked=(unusable.get(case.get('solver_key')) or unusable.get('*')) if solver else None
        if blocked:
            # consumer reported the operation unusable at install time: asking the agent cannot pass.
            self.save_case(owner,rid,index,solver_validation.settle(case,solver_validation.unavailable_result(blocked),''))
            return
        if not case.get('answer'):
            while True:
                if not case.get('remote_id'):
                    profile=self.remote(owner,rid,lambda t: self.consumer.profile(t,ws,run.get('model_profile_id','')))
                    if profile!=run['model']:
                        raise Failure('configuration_changed','问答模型发生变化',409)
                    case['status']='unknown'
                    self.save_case(owner,rid,index,case)
                    case['remote_id']=self.remote(owner,rid,lambda t: self.submit(t,rid,run,index,case))
                    case['status']='executing'
                    case.pop('error',None)
                    self.save_case(owner,rid,index,case)
                deadline=time.monotonic()+REMOTE_SECONDS
                remote_path=f"/runs/{quote(case['remote_id'],safe='')}"
                while True:
                    if self.stopping: return
                    if time.monotonic()>deadline:
                        raise Failure('remote_timeout','消费运行 15 分钟仍未结束，请稍后恢复对账',504,'cases')
                    cancelling=self.store.get(owner,rid,'runs').get('cancel_requested',False)
                    if cancelling:
                        self.remote(owner,rid,lambda t: self.consumer.call('POST',f'{remote_path}/cancel',t,ws,json={},headers=self.consumer.traffic()))
                    response=self.remote(owner,rid,lambda t: self.consumer.call('GET',remote_path,t,ws))
                    if response['status'] in TERMINAL: break
                    time.sleep(1)
                attempt=case.get('attempt',0)+1
                if not prepare_failed(response) or attempt>=SESSION_ATTEMPTS: break
                # Nothing is in flight while waiting, so a stop or cancellation here simply leaves the case pending.
                case.update(attempt=attempt,status='pending')
                del case['remote_id']
                self.save_case(owner,rid,index,case)
                if not self.backoff(owner,rid,retry_delay(attempt)): return
                token=self.token(owner,rid)
            token=self.token(owner,rid)
            if solver:
                self.settle_solver(owner,rid,ws,index,case,response,token)
                return
            trace=self.consumer.trace(token,ws,case['remote_id'])
            calls,omitted=trace.pop('calls',None),trace.pop('omitted_calls',0)
            if calls is not None:
                # Kept beside the run record: the run detail page reads the whole record every few seconds.
                self.store.put_evidence(owner,rid,index,{'calls':calls,'omitted':omitted})
            case['trace']=trace
            case['evidence']={'remote_run_id':case['remote_id'],'status':response['status'],
                              'trace_status':'collected' if trace['status']=='complete' else 'not_collected'}
            answer=final_answer(response)
            if response['status']!='completed' or not answer:
                case['status']='cancelled' if response['status']=='cancelled' else 'execution_error'
                case['error']=consumer_failure(response)
                if case.get('attempt'): case['error']['message']+=f'（已换新会话重试 {case["attempt"]} 次）'
                self.save_case(owner,rid,index,case)
                return
            case['answer']=answer
            case['status']='answer_ready'
            case.pop('error',None)
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

    def submit(self,token,rid,run,index,case):
        """Consumer /runs persists the session fingerprint and idempotency key, so reconciliation resubmits the exact same
        request of one attempt; only a retry after a failed session preparation starts a new session."""
        suffix=str(index)+(f'-{case["attempt"]}' if case.get('attempt') else '')
        body={'request':case['question'],'session_id':'eval-'+rid+'-'+suffix,
              'model_profile_id':run['model']['id'],'include_trace':True}
        if run.get('run_type')=='solver':
            # Pin the exact activated version so the agent cannot pick another package in the workspace.
            body['arguments']={'selected_ontology_id':run['package_ref']['ontology_id'],
                               'selected_ontology_version':run['package_ref']['version']}
        response=self.consumer.call('POST','/runs',token,run['workspace'],
            headers={'Idempotency-Key':rid+'-'+suffix,**self.consumer.traffic()},json=body)
        return response['run_id']

    def settle_solver(self,owner,rid,ws,index,case,remote,token):
        """Judge one solver validation case from its run's public events; consumer keeps the full trace."""
        try:
            calls=solver_validation.observe(self.consumer.events(token,ws,case['remote_id'],solver_validation.EVENT_LIMIT))
        except Failure as exc:
            case.update(status='execution_error',error={'code':'solver_events_unavailable','phase':'cases',
                        'message':f'无法读取 consumer 运行事件，未能取证：{exc.message}'})
            self.save_case(owner,rid,index,case)
            return
        result=solver_validation.case_result(case,calls,remote)
        if solver_validation.has_candidate_results(result):
            result.update(self.consumer.candidate_count(token,ws,result['job_id']))
        solver_validation.settle(case,result,final_answer(remote))
        case['evidence']={'remote_run_id':case['remote_id'],'status':remote['status'],'trace_status':'collected'}
        self.save_case(owner,rid,index,case)

    def backoff(self,owner,rid,seconds):
        """Wait before a retry; False as soon as the service stops or the run is being cancelled."""
        until=time.monotonic()+seconds
        while True:
            if self.stopping or self.store.get(owner,rid,'runs').get('cancel_requested'): return False
            left=until-time.monotonic()
            if left<=0: return True
            time.sleep(min(1,left))

    def _cancel(self,owner,rid,run):
        """Cancellation does not require Judge, mapping, database or model readiness."""
        for index,case in enumerate(run['cases']):
            if case['status'] in SETTLED or case.get('answer'):
                continue
            token=self.token(owner,rid)
            if not case.get('remote_id') and case['status']=='unknown':
                case['remote_id']=self.submit(token,rid,run,index,case)
                self.update(owner,rid,cases=run['cases'])
            if case.get('remote_id'):
                path=f"/runs/{quote(case['remote_id'],safe='')}"
                self.consumer.call('POST',f'{path}/cancel',token,run['workspace'],json={},headers=self.consumer.traffic())
                deadline=time.monotonic()+30
                while True:
                    if self.stopping: return
                    token=self.token(owner,rid)
                    remote=self.consumer.call('GET',path,token,run['workspace'])
                    if remote['status'] in TERMINAL: break
                    if time.monotonic()>deadline:
                        raise Failure('cancel_pending','远端尚未确认取消，请恢复对账',504,'cancelling')
                    time.sleep(1)
                if remote['status']=='completed' and run.get('run_type')=='solver':
                    # Settles this same case object, so the cases saved below include it.
                    self.settle_solver(owner,rid,run['workspace'],index,case,remote,token)
                    continue
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
