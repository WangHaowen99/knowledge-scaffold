import { useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { toast } from '../../app/toast.jsx';
import { isLocked } from '../../state/readonly.js';
import {
  Area, Button, Card, Chip, Empty, Field, Icon, Inp, Note, Select, Toggle, cx,
} from '../../ui/index.jsx';
import { ScopeLockBar, SendButton } from '../plugin/parts.jsx';
import {
  DATA_ACCESS_OPTIONS, draftKey, draftOf, findingText, hasDraftErrors, isDirty, newContract,
  payloadOf, responseMode, termSource, testVectorsPatch, verificationText,
  assistantDraftCommand, businessExamples, executionNextStep, selectionAfterRefresh,
} from './deterministicModel.js';

const FILES = [
  ['parser_source', 'Parser', 'parser.py', '把自然语言请求转换为结构化参数；不能读取工作空间数据。'],
  ['input_schema', '输入 Schema', 'input.schema.json', '声明 Handler 接收的参数、必填项和字段类型。'],
  ['output_schema', '输出 Schema', 'output.schema.json', '声明权威执行结果的结构和字段类型。'],
  ['test_vectors_source', '测试向量', 'tests.json', '用合成自然语言、预期参数和预期输出验证 Parser→Schema→Handler。'],
];

const testSource = (item) => item?.test_vectors_source
  ?? JSON.stringify(item?.test_vectors || [], null, 2);

export default function DeterministicTab({ view, lock, tasks, onSaved, reload }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [base, setBase] = useState(null);
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(0);
  const [fileRole, setFileRole] = useState('parser_source');
  const [live, setLive] = useState(null);
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [newer, setNewer] = useState(false);
  const [candidates, setCandidates] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [drafting, setDrafting] = useState('');
  const [draftFailure, setDraftFailure] = useState(null);
  const nextViewMode = useRef('auto');
  const dirtyRef = useRef(false);
  const editVersion = useRef(0);
  const baseRevision = useRef('');
  const candidateRequest = useRef(0);
  const draftingRef = useRef('');
  const mutationRef = useRef('');
  const projectRef = useRef(pid);
  if (projectRef.current !== pid) {
    projectRef.current = pid;
    candidateRequest.current += 1;
    draftingRef.current = '';
  }
  const dirty = base ? isDirty(base, items) : false;
  const draftErrors = hasDraftErrors(items);
  dirtyRef.current = dirty;

  useEffect(() => {
    if (!view) return;
    const mode = nextViewMode.current;
    nextViewMode.current = 'auto';
    if (mode === 'refresh-preserve') {
      setNewer((view.revision || '') !== baseRevision.current);
      return;
    }
    if (mode === 'preserve') {
      setBase(view);
      baseRevision.current = view.revision || '';
      setNewer(false);
      return;
    }
    if (mode === 'replace' || !base || !dirtyRef.current) {
      setBase(view);
      baseRevision.current = view.revision || '';
      setItems(draftOf(view));
      setSelected((value) => selectionAfterRefresh(items, value, view.operations || []));
      setSaveError('');
      setNewer(false);
    } else if ((view.revision || '') !== baseRevision.current) setNewer(true);
  }, [view]);

  useEffect(() => {
    projectRef.current = pid;
    candidateRequest.current += 1;
    draftingRef.current = '';
    setCandidates(null);
    setDrafting('');
    setDraftFailure(null);
    return () => {
      projectRef.current = null;
      candidateRequest.current += 1;
      draftingRef.current = '';
    };
  }, [pid]);

  const key = useMemo(() => draftKey(items), [items]);
  useEffect(() => {
    if (items.length && selected >= items.length) setSelected(items.length - 1);
  }, [items.length, selected]);

  useEffect(() => {
    if (!base) return undefined;
    if (draftErrors) {
      setLive({ key, ready: false, findings: [], error: '存在未完成的测试向量 JSON，请修复后再保存' });
      return undefined;
    }
    if (!items.length) { setLive({ key, ready: true, findings: [] }); return undefined; }
    let alive = true;
    const timer = setTimeout(() => {
      api.checkDeterministicContract(pid, { operations: payloadOf(items) })
        .then((result) => { if (alive) setLive({ key, ...result }); })
        .catch((error) => { if (alive) setLive({ key, ready: false, findings: [], error: errorText(error) }); });
    }, 400);
    return () => { alive = false; clearTimeout(timer); };
  }, [api, base, draftErrors, items.length, key, pid]);

  if (!view || !base) return <div className="card"><div className="card-b muted sm">正在读取…</div></div>;

  const current = items[Math.min(selected, Math.max(0, items.length - 1))] || null;
  const checked = live?.key === key;
  const findings = checked ? (live?.findings || []) : [];
  const managedTargets = [
    'ext/deterministic-execution/contracts',
    'ext/deterministic-execution/contracts/example/contract.json',
    ...items.map((item) => `ext/deterministic-execution/contracts/${item.key || 'unnamed'}/contract.json`),
  ];
  const locked = managedTargets.some((path) => isLocked(lock, path));
  const busy = saving || verifying;
  const markEdited = () => { editVersion.current += 1; };
  const update = (patch) => {
    if (mutationRef.current) return;
    markEdited();
    setItems((list) => list.map((item, index) => (index === selected ? { ...item, ...patch } : item)));
    setSaveError('');
  };
  const updateFile = (role, value) => {
    if (role !== 'test_vectors_source') { update({ [role]: value }); return; }
    update(testVectorsPatch(value));
  };
  const add = () => {
    if (mutationRef.current) return;
    markEdited();
    const item = newContract({ ...base, operations: items }, items.length);
    setItems((list) => [...list, item]);
    setSelected(items.length);
  };
  const remove = () => {
    if (mutationRef.current) return;
    markEdited();
    setItems((list) => list.filter((_, index) => index !== selected));
    setSelected((value) => Math.max(0, value - 1));
  };
  const discard = () => {
    if (mutationRef.current) return;
    setItems(draftOf(base)); setSelected(0); setSaveError(''); setNewer(false);
  };
  const refresh = () => {
    const requestEditVersion = editVersion.current;
    reload({
      beforeApply: () => {
        const mode = responseMode(requestEditVersion, editVersion.current);
        nextViewMode.current = mode === 'preserve' ? 'refresh-preserve' : mode;
      },
    });
  };
  const scan = async () => {
    if (busy) return;
    const projectId = pid;
    const requestId = candidateRequest.current + 1;
    candidateRequest.current = requestId;
    setScanning(true);
    setSaveError('');
    try {
      const result = await api.deterministicCandidates(pid);
      if (candidateRequest.current === requestId && projectRef.current === projectId) setCandidates(result);
    } catch (error) {
      if (candidateRequest.current === requestId && projectRef.current === projectId) setSaveError(errorText(error));
    } finally {
      if (candidateRequest.current === requestId && projectRef.current === projectId) setScanning(false);
    }
  };
  const useCandidate = async (candidate, replace = false) => {
    if (busy || draftingRef.current || (!replace && items.some((item) => item.operation === candidate.operation))) return;
    const projectId = pid;
    draftingRef.current = candidate.operation;
    setDrafting(candidate.operation);
    setSaveError('');
    setDraftFailure(null);
    try {
      const result = await api.createDeterministicDraft(pid, {
        operation: candidate.operation, handler: candidate.handler, data_access: candidate.data_access,
      });
      if (projectRef.current !== projectId || draftingRef.current !== candidate.operation) return;
      const draft = { ...result.draft, draft_notices: result.notices || [] };
      markEdited();
      setItems((list) => {
        const existing = list.findIndex((item) => item.operation === candidate.operation);
        if (existing >= 0) {
          setSelected(existing);
          return replace ? list.map((item, index) => index === existing ? draft : item) : list;
        }
        const next = [...list, draft];
        setSelected(next.length - 1);
        return next;
      });
      // CandidateList derives configured state from the current drafts. Keep the
      // scan result intact so discarding a draft makes it available again.
      toast(result.draft_origin === 'agent'
        ? '已读取助手草稿，请确认业务示例后保存并验证'
        : '基础草稿已生成；如需完善，请在对话中让助手完成后重新制作草稿', 'check');
    } catch (error) {
      if (projectRef.current === projectId) {
        const details = Array.isArray(error.details?.findings) ? error.details.findings : [];
        const message = [errorText(error), ...findingText(details)].join('\n');
        setSaveError(message);
        setDraftFailure({ candidate, message, findings: details.length ? details : [{ message }] });
      }
    } finally {
      if (projectRef.current === projectId) {
        draftingRef.current = '';
        setDrafting('');
      }
    }
  };
  const save = async () => {
    if (mutationRef.current) return;
    if (draftErrors) {
      setSaveError('存在未完成的测试向量 JSON，请修复后再保存');
      return;
    }
    const requestEditVersion = editVersion.current;
    mutationRef.current = 'save';
    setSaving(true);
    setSaveError('');
    try {
      const result = await api.saveDeterministicContract(pid, {
        base_revision: base.revision || '', operations: payloadOf(items),
      });
      const mode = responseMode(requestEditVersion, editVersion.current);
      nextViewMode.current = mode;
      if (mode === 'preserve') {
        setBase(result);
        baseRevision.current = result.revision || '';
      }
      onSaved(result);
      toast(result.commit ? '确定性执行契约已保存并生成项目版本' : '确定性执行契约已保存', 'check');
    } catch (error) {
      setSaveError(error?.code === 'deterministic_contract_revision_conflict'
        ? '契约已在别处更新，请刷新后重试' : errorText(error));
    } finally {
      mutationRef.current = '';
      setSaving(false);
    }
  };
  const verify = async () => {
    if (mutationRef.current) return;
    const requestEditVersion = editVersion.current;
    mutationRef.current = 'verify';
    setVerifying(true);
    setSaveError('');
    try {
      const result = await api.verifyDeterministicContract(pid, current.key, {
        base_revision: base.revision || '',
      });
      const mode = responseMode(requestEditVersion, editVersion.current);
      nextViewMode.current = mode;
      if (mode === 'preserve') {
        setBase(result.contract);
        baseRevision.current = result.contract.revision || '';
      }
      onSaved(result.contract);
      toast(result.report?.status === 'passed' ? '确定性执行闭包验证通过' : '确定性执行闭包验证未通过',
        result.report?.status === 'passed' ? 'check' : 'alert');
    } catch (error) {
      setSaveError(errorText(error));
    } finally {
      mutationRef.current = '';
      setVerifying(false);
    }
  };

  if (!items.length) {
    return (
      <>
        <Card title="确定性 Operation" icon="cpu" sub="选择适合固定计算或实时查询的业务操作，让助手完成实现，你只需确认业务示例">
          <Empty
            icon="cpu"
            title={dirty ? "将删除全部确定性执行契约" : "还没有确定性执行契约"}
            actions={dirty ? (
              <>
                <Button kind="ghost" disabled={saving} onClick={discard}>放弃删除</Button>
                <Button kind="ghost" icon="refresh" disabled={busy} onClick={refresh}>刷新</Button>
                <Button kind="primary" disabled={newer || locked || saving || !checked} onClick={save}>保存删除</Button>
              </>
            ) : (
              <>
                <Button kind="ghost" icon="refresh" disabled={busy} onClick={refresh}>刷新</Button>
                <Button kind="ghost" icon="scan" disabled={scanning || busy} onClick={scan}>{scanning ? '扫描中…' : '扫描候选'}</Button>
                {tasks ? <SendButton tasks={tasks} command="/ontology-deterministic-scan" text="请推荐适合确定性执行的业务操作，说明理由，由我确认后再制作。" disabled={locked || busy || !!tasks.active}>让助手推荐</SendButton> : null}
                <Button kind="ghost" icon="plus" disabled={locked || busy || !!drafting} onClick={add}>高级：手工制作</Button>
              </>
            )}
          >
            先让助手推荐适合固定计算或实时查询的业务操作，再选择制作；缺少实现时由助手补齐，你只需确认业务示例。
          </Empty>
        </Card>
        {saveError ? <Note tone="red" className="mt12">{saveError}</Note> : null}
        {draftFailure && tasks ? <div className="mt12"><SendButton tasks={tasks} {...assistantDraftCommand(draftFailure.candidate, draftFailure.findings)} disabled={locked || busy || !!tasks.active}>让助手修复读取问题</SendButton></div> : null}
        {newer && !saveError ? <Note tone="amber" className="mt12">契约已在别处更新，请刷新后再保存。</Note> : null}
        {candidates ? <CandidateList data={candidates} operations={items} locked={locked || busy} drafting={drafting} onUse={useCandidate} tasks={tasks} /> : null}
      </>
    );
  }

  const operationOptions = (base.available_operations || []).map((value) => ({ value, label: value }));
  const handlerOptions = (base.available_handlers || []).map((value) => ({ value, label: `${value}.py` }));
  if (current.operation && !operationOptions.some((item) => item.value === current.operation)) operationOptions.push({ value: current.operation, label: `${current.operation}（不存在）` });
  if (current.handler && !handlerOptions.some((item) => item.value === current.handler)) handlerOptions.push({ value: current.handler, label: `${current.handler}.py（不存在）` });
  const file = FILES.find(([role]) => role === fileRole) || FILES[0];
  const fileValue = fileRole === 'test_vectors_source'
    ? (current.test_vectors_source ?? testSource(current)) : current[fileRole];
  const verification = current.verification || {};
  const examples = businessExamples(current);
  const nextStep = executionNextStep({ dirty, newer, checking: !checked, findings: findings.length || live?.error, verification, enabled: current.enabled });

  return (
    <>
      <ScopeLockBar
        lock={lock}
        targets={managedTargets}
        text={{ running: ['助手正在修改确定性执行契约，暂时只读'], pending: ['确定性执行契约有待确认的修改'] }}
      />
      <div className="mr-2c w mt16">
        <div className={cx('card', locked && 'mr-ro')}>
          <div className="card-h">
            <Icon name="cpu" /><h3>{current.operation || current.key || '新的确定性 Operation'}</h3>
            <Chip tone={current.enabled ? 'blue' : 'gray'} sm>{current.enabled ? '已启用' : '已停用'}</Chip>
            <span className="sp" />
            <span className="row g4 sm">随知识包发布 <Toggle on={current.enabled} disabled={locked || busy} onChange={(enabled) => update({ enabled })} /></span>
          </div>
          <div className="card-b">
            <Note tone="blue">{nextStep}</Note>
            <div className="row wrap g4 mt12">
              {tasks ? <SendButton tasks={tasks} {...assistantDraftCommand(current, findings, verification)} disabled={locked || busy || !!drafting || !!tasks.active}>让助手完善</SendButton> : null}
              {draftFailure && tasks ? <SendButton tasks={tasks} {...assistantDraftCommand(draftFailure.candidate, draftFailure.findings)} disabled={locked || busy || !!tasks.active}>让助手修复读取问题</SendButton> : null}
              <Button kind="ghost" disabled={locked || busy || !!drafting} onClick={() => useCandidate(current, true)}>{drafting ? '读取中…' : '读取助手新版草稿'}</Button>
            </div>
            <div className="sm muted mt8">读取新版会替换当前操作的本地草稿；确认保存前不会修改正式契约。助手任务进度显示在页头，可前往对话补充业务要求。</div>
            <h4>业务示例与预期结果</h4>
            <div className="sm muted">以下是待确认的预期结果，不是本次实际执行结果。代码和数据规则由助手完善。</div>
            {examples.map((example, index) => (
              <div key={index} className="mt12 sm">
                <strong>{example.name || `示例 ${index + 1}`}</strong>
                <div>用户提问：{example.text}</div>
                {example.known ? <div>此前已确认的参数（优先使用）：{example.known}</div> : null}
                <div>预期：{example.expected}</div>
              </div>
            ))}
            {!examples.length ? <Note tone="amber">尚无有效业务示例，请让助手补充。</Note> : null}
            <details className="mt16">
              <summary>高级详情：实现、输入输出规则与测试代码</summary>
            <div className="grid2" style={{ gap: 12 }}>
              <Field label="绑定的 Operation" req><Select value={current.operation} options={operationOptions} placeholder="选择 Operation" disabled={locked || busy} onChange={(operation) => update({ operation })} /></Field>
              <Field label="确定性标识" req hint="小写字母开头，只含小写字母、数字和下划线"><Inp mono value={current.key} disabled={locked || busy} onChange={(event) => update({ key: event.target.value })} /></Field>
              <Field label="Handler" req hint="执行固定计算的 Python 脚本；须与所选 Operation 绑定"><Select value={current.handler} options={handlerOptions} placeholder="选择 Handler" disabled={locked || busy} onChange={(handler) => update({ handler })} /></Field>
              <Field label="数据访问" req hint="只声明能力，不绑定 Neo4j 等具体 Provider 或凭据"><Select value={current.data_access} options={DATA_ACCESS_OPTIONS} disabled={locked || busy} onChange={(data_access) => update({ data_access })} /></Field>
              <Field label="别名" hint="多个别名用逗号或换行分隔"><Area minHeight={64} textareaStyle={{ height: 44 }} value={termSource(current, 'aliases')} disabled={locked || busy} onChange={(event) => update({ aliases_source: event.target.value })} /></Field>
              <Field label="路由关键词" hint="命中其中任意一个即可作为候选"><Area minHeight={64} textareaStyle={{ height: 44 }} value={termSource(current, 'any_terms')} disabled={locked || busy} onChange={(event) => update({ any_terms_source: event.target.value })} /></Field>
              <Field label="必须同时命中的关键词" hint="留空表示不增加同时命中条件" className="span2"><Area minHeight={64} textareaStyle={{ height: 44 }} value={termSource(current, 'all_terms')} disabled={locked || busy} onChange={(event) => update({ all_terms_source: event.target.value })} /></Field>
            </div>
            <div className="row mt16 wrap g4">
              {FILES.map(([role, label, name]) => (
                <button type="button" key={role} disabled={busy} className={cx('mr-file scr-file', fileRole === role && 'on')} onClick={() => setFileRole(role)}>
                  <Icon name="file" size={12} /><span>{label}</span><span className="mono xs">{name}</span>
                </button>
              ))}
            </div>
            <div className="xs muted mt8">{file[3]}</div>
            <Area className="mt8 scr-area" spellCheck={false} disabled={locked || busy} value={fileValue} textareaStyle={{ height: fileRole === 'parser_source' ? 260 : 300 }} onChange={(event) => updateFile(fileRole, event.target.value)} />
            {current.test_vectors_error && fileRole === 'test_vectors_source' ? <Note tone="red" className="mt8">{current.test_vectors_error}</Note> : null}
            </details>
          </div>
        </div>
        <div className="col" style={{ gap: 16 }}>
          <Card title="检查与保存" icon="shieldok">
            {!checked ? <div className="row g4 sm"><span className="spin" />正在检查草稿…</div> : null}
            {checked && live?.error ? <Note tone="red">{live.error}</Note> : null}
            {checked && !live?.error ? (
              <div className="col sm" style={{ gap: 7 }}>
                <div className="row g4"><Icon name={findings.length ? 'x' : 'check'} size={14} /><Chip tone={findings.length ? 'red' : 'teal'} sm>{findings.length ? `${findings.length} 项未通过` : '契约检查通过'}</Chip></div>
                {findingText(findings).map((text, index) => <div key={index} style={{ color: 'var(--red)' }}>· {text}</div>)}
                <div className="row g4 mt8"><Chip tone={!dirty && verification.status === 'passed' && verification.current ? 'teal' : 'amber'} sm>{dirty ? '当前草稿尚未验证' : verificationText(verification)}</Chip></div>
                {!dirty && verification.current && verification.cases?.map((item, index) => <div key={`result:${index}`}>{item.ok ? '✓' : '✗'} {item.name}：{item.ok ? '与预期一致' : item.reason}</div>)}
                {verification.cases?.filter((item) => !item.ok).map((item, index) => <div key={`verify:${index}`} style={{ color: 'var(--red)' }}>· {item.name}：{item.reason}</div>)}
              </div>
            ) : null}
            {saveError ? <Note tone="red" className="mt12">{saveError}</Note> : null}
            {newer && !saveError ? <Note tone="amber" className="mt12">契约已在别处更新，请刷新后再保存。</Note> : null}
            {(current.draft_notices || []).map((text) => <Note key={text} tone="amber" className="mt12">{text}</Note>)}
            <div className="row mt16">
              <Button size="sm" kind="ghost" disabled={!dirty || locked || busy} onClick={discard}>放弃修改</Button>
              <Button size="sm" kind="ghost" icon="refresh" disabled={busy} onClick={refresh}>刷新</Button>
              <span className="sp" />
              <Button size="sm" kind="ghost" disabled={dirty || newer || locked || busy || !current.enabled} onClick={verify}>{verifying ? '真实沙箱验证中，请稍候…' : '验证执行闭包'}</Button>
              <Button size="sm" kind="primary" disabled={!dirty || newer || locked || busy || draftErrors || !checked || !!live?.error || findings.length > 0} onClick={save}>{saving ? '保存中…' : '确认业务并保存契约'}</Button>
            </div>
          </Card>
          <Card title="其他确定性 Operation" icon="list">
            {items.map((item, index) => (
              <div key={`${item.key}:${index}`} className={cx('row sm scr-other', index === selected && 'on')} onClick={() => { if (!busy) setSelected(index); }}>
                <span className="grow">{item.operation || item.key || `未命名 ${index + 1}`}</span>
                <Chip tone={item.data_access === 'workspace_provider' ? 'blue' : 'line'} sm>{item.data_access === 'workspace_provider' ? '工作空间数据' : '仅输入参数'}</Chip>
              </div>
            ))}
            <div className="row mt12">
              <Button size="sm" kind="ghost" icon="scan" disabled={scanning || busy} onClick={scan}>{scanning ? '扫描中…' : '扫描候选'}</Button>
              <Button size="sm" icon="plus" disabled={locked || busy || !!drafting} onClick={add}>制作另一个</Button>
              <span className="sp" />
              <Button size="sm" kind="ghost" icon="trash" disabled={locked || busy || !!drafting} onClick={remove}>删除当前契约</Button>
            </div>
          </Card>
          {candidates ? <CandidateList data={candidates} operations={items} locked={locked || busy} drafting={drafting} onUse={useCandidate} tasks={tasks} /> : null}
          <Card title="发布与试用" icon="upload"><div className="sm muted">全部启用的操作验证通过后，到发布页面检查并发布知识包。在消费端选择该包的已发布版本，尝试上面的业务问题。读取工作空间数据的操作还需在消费端配置数据连接；沙箱中的合成数据不代表实时数据。</div></Card>
        </div>
      </div>
    </>
  );
}

const CANDIDATE_LABELS = {
  recommended: ['推荐', 'teal'], needs_info: ['待补充', 'amber'],
  not_recommended: ['不推荐', 'red'], configured: ['已配置', 'blue'],
};

function CandidateList({ data, operations, locked, drafting, onUse, tasks }) {
  const localOperations = new Set((operations || []).map((item) => item.operation));
  const candidates = (data.candidates || []).map((item) => (
    localOperations.has(item.operation)
      ? { ...item, recommendation: 'configured', can_create: false }
      : item
  ));
  const counts = candidates.reduce((result, item) => ({
    ...result, [item.recommendation]: (result[item.recommendation] || 0) + 1,
  }), {});
  return (
    <Card title="确定性候选" icon="scan" sub="扫描实现是否齐备；业务上是否适合制作，可先让助手推荐">
      <div className="col" style={{ gap: 10 }}>
        <Note>“待补充”表示实现尚不齐备，并不表示该操作一定适合确定性执行。固定计算、受限实时查询可按业务需要制作；纯规则解释可以保留普通回答。</Note>
        <div className="row wrap g4 sm">
          <Chip tone="teal" sm>推荐 {counts.recommended || 0}</Chip>
          <Chip tone="amber" sm>待补充 {counts.needs_info || 0}</Chip>
          <Chip tone="red" sm>不推荐 {counts.not_recommended || 0}</Chip>
          <Chip tone="blue" sm>已配置 {counts.configured || 0}</Chip>
        </div>
        {(data.solver_findings || []).map((text) => (
          <Note key={text} tone="amber">Solver 契约检查：{text}</Note>
        ))}
        {candidates.map((item) => {
          const [label, tone] = CANDIDATE_LABELS[item.recommendation] || [item.recommendation, 'line'];
          return (
            <div key={item.operation} className="card-b" style={{ border: '1px solid var(--line)', borderRadius: 10 }}>
              <div className="row g4"><strong>{item.operation}</strong><Chip tone={tone} sm>{label}</Chip><span className="sp" />
                {tasks && item.recommendation !== 'configured' && item.recommendation !== 'not_recommended' ? <SendButton tasks={tasks} {...assistantDraftCommand(item)} disabled={locked || !!drafting || !!tasks.active}>选择并让助手制作</SendButton> : null}
                {item.can_create ? <Button size="sm" kind="primary" disabled={locked || !!drafting} onClick={() => onUse(item)}>{drafting === item.operation ? '生成中…' : '制作草稿'}</Button> : null}
                {!item.can_create && item.handler && item.recommendation === 'needs_info' ? <Button size="sm" kind="ghost" disabled={locked || !!drafting || !!tasks?.active} onClick={() => onUse(item)}>{drafting === item.operation ? '读取中…' : '读取助手草稿'}</Button> : null}
              </div>
              {item.handler ? <div className="xs muted mt8">Handler：<span className="mono">{item.handler}.py</span> · {item.data_access === 'workspace_provider' ? '读取工作空间数据' : '仅使用输入参数'}</div> : null}
              {(item.reasons || []).map((text) => <div key={`reason:${text}`} className="sm mt8">✓ {text}</div>)}
              {(item.blockers || []).map((text) => <div key={`block:${text}`} className="sm mt8" style={{ color: 'var(--red)' }}>· {text}</div>)}
              {(item.confirmations || []).map((text) => <div key={`confirm:${text}`} className="xs muted mt8">待确认：{text}</div>)}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
