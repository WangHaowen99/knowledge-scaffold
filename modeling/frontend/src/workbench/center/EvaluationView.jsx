import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Check, ChevronRight, Database, Download, FileJson, LoaderCircle, Pencil, Plus, RefreshCw, Save, Search, Send, Sparkles, Trash2, Upload, X } from 'lucide-react';
import DataSourcePanel from '../../DataSourcePanel.jsx';
import { EvaluationResultsList } from '../../EvaluationResultCard.jsx';
import { parseTestcaseImport, testcaseImportTemplateJson } from '../../testcase-import.js';
import { actions, store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import {
  addEvaluationTestcase, addEvaluationTestcases, addManualEvaluationSuggestion, applyEvaluationSuggestions,
  cancelEvaluationRun,
  confirmEvaluationDiagnoses, deleteEvaluationTestcase, diagnoseEvaluation, getEvaluationDataSource,
  getEvaluationOptimizationStatus, getEvaluationRound, listEvaluationDiagnoses, listEvaluationRounds, listEvaluationSuggestions,
  listEvaluationTestcases, proposeEvaluationOptimizations, rejectEvaluationSuggestions, saveEvaluationDataSource,
  reviewEvaluationOptimization, startEvaluation, updateEvaluationSuggestion, updateEvaluationTestcase,
} from '../lib/api.js';

const TABS = [
  ['setup', '准备'], ['cases', '真实用例'], ['runs', '运行结果'],
  ['diagnose', '失败诊断'], ['optimize', '优化建议'],
];
const STATUS = { running: ['运行中', 'wait'], done: ['已完成', 'ok'], failed: ['运行失败', 'stale'] };
const KINDS = [
  ['modify_rule', '修改业务规则'], ['add_rule', '新增业务规则'], ['modify_operation', '修改操作'],
  ['add_operation', '新增操作'], ['modify_object', '修改对象'], ['add_object', '新增对象'],
  ['modify_metric', '修改指标'], ['add_metric', '新增指标'], ['modify_script', '修改脚本'],
  ['add_script', '新增脚本'], ['clarify_description', '澄清描述'],
];

export default function EvaluationView() {
  const projectId = useStore(store, (state) => state.projectId);
  const [tab, setTab] = useState('setup');
  const [facts, setFacts] = useState({ source: undefined, cases: undefined, rounds: undefined });
  const [round, setRound] = useState(null);
  const [followup, setFollowup] = useState({
    roundId: '', diagnoses: [], suggestions: [], runningOperation: '', runningTask: null,
    loading: false, error: '',
  });
  const [followupRefresh, setFollowupRefresh] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const loadFacts = useCallback(async () => {
    if (!projectId) return;
    try {
      const [source, cases, rounds] = await Promise.all([getEvaluationDataSource(projectId), listEvaluationTestcases(projectId), listEvaluationRounds(projectId)]);
      setFacts({ source: source.dataSource || null, cases: cases.testcases || [], rounds: rounds.rounds || [] }); setError('');
    } catch (cause) { setError(cause.message || '评测事实读取失败'); }
  }, [projectId]);
  const openRound = useCallback(async (roundId) => {
    if (!projectId || !roundId) return;
    try {
      setRound(await getEvaluationRound(projectId, roundId)); setError('');
    } catch (cause) { setError(cause.message || '评测结果读取失败'); }
  }, [projectId]);
  useEffect(() => { void loadFacts(); }, [loadFacts]);
  useEffect(() => { if (!round && facts.rounds?.[0]) void openRound(facts.rounds[0].id); }, [facts.rounds, openRound, round]);
  useEffect(() => {
    const timer = window.setInterval(() => { void loadFacts(); if (round?.round?.status === 'running') void openRound(round.round.id); }, 3000);
    return () => window.clearInterval(timer);
  }, [loadFacts, openRound, round?.round?.id, round?.round?.status]);
  useEffect(() => {
    if (!['diagnose', 'optimize'].includes(tab)) return undefined;
    const roundId = round?.round?.id;
    const terminal = ['done', 'failed'].includes(round?.round?.status);
    if (!projectId || !roundId || !terminal) {
      setFollowup({
        roundId: roundId || '', diagnoses: [], suggestions: [], runningOperation: '', runningTask: null,
        loading: false, error: '',
      });
      return undefined;
    }
    let active = true;
    setFollowup((old) => {
      const sameRound = old.roundId === roundId;
      return {
        roundId,
        diagnoses: sameRound ? old.diagnoses : [],
        suggestions: sameRound ? old.suggestions : [],
        runningOperation: sameRound ? old.runningOperation : '',
        runningTask: sameRound ? old.runningTask : null,
        loading: !sameRound,
        error: '',
      };
    });
    void (async () => {
      try {
        const [diagnosisData, suggestionData] = await Promise.all([
          listEvaluationDiagnoses(projectId, roundId),
          tab === 'optimize' ? listEvaluationSuggestions(projectId, roundId) : Promise.resolve(null),
        ]);
        if (!active) return;
        setFollowup((old) => ({
          roundId,
          diagnoses: diagnosisData.diagnoses || [],
          suggestions: suggestionData ? (suggestionData.suggestions || []) : (old.roundId === roundId ? old.suggestions : []),
          runningOperation: diagnosisData.runningOperation || suggestionData?.runningOperation || '',
          runningTask: diagnosisData.runningTask || suggestionData?.runningTask || null,
          loading: false,
          error: '',
        }));
      } catch (cause) {
        if (!active) return;
        setFollowup((old) => ({ ...old, roundId, loading: false, error: cause.message || '评测后续信息读取失败' }));
      }
    })();
    return () => { active = false; };
  }, [followupRefresh, projectId, round?.round?.id, round?.round?.status, tab]);
  const official = useMemo(() => (facts.cases || []).filter((item) => item.source === 'manual'), [facts.cases]);
  const currentFollowup = followup.roundId === round?.round?.id ? followup : {
    diagnoses: [], suggestions: [], runningOperation: '', runningTask: null, loading: false, error: '',
  };
  const counts = {
    cases: official.length,
    runs: facts.rounds?.length || 0,
    diagnose: currentFollowup.diagnoses.length,
    optimize: currentFollowup.suggestions.filter((item) => item.status === 'pending').length,
  };
  const loading = facts.source === undefined || facts.cases === undefined || facts.rounds === undefined;
  const followupTab = ['diagnose', 'optimize'].includes(tab);
  const terminalRound = ['done', 'failed'].includes(round?.round?.status);
  const pollingFollowup = terminalRound && (
    (tab === 'diagnose' && currentFollowup.runningOperation === 'diagnose')
    || (tab === 'optimize' && ['propose', 'apply'].includes(currentFollowup.runningOperation))
  );
  useEffect(() => {
    if (!pollingFollowup) return undefined;
    const timer = window.setTimeout(() => setFollowupRefresh((value) => value + 1), 2000);
    return () => window.clearTimeout(timer);
  }, [followupRefresh, pollingFollowup]);
  const refresh = () => {
    void loadFacts();
    if (round?.round?.id) void openRound(round.round.id);
    if (followupTab) setFollowupRefresh((value) => value + 1);
  };
  return <div className="wb-eval">
    <div className="tabs wb-eval-tabs" role="tablist" aria-label="评测与优化工作区">
      {TABS.map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{label}{counts[id] > 0 && <span className="wb-eval-count">{counts[id]}</span>}</button>)}
      <span className="spacer" /><button type="button" className="wb-eval-refresh" title="刷新" aria-label="刷新" onClick={refresh}><RefreshCw size={15} /></button>
    </div>
    {error && <div className="wb-eval-alert stale">{error}</div>}
    {notice && <div className="wb-eval-alert ok"><Check size={15} />{notice}</div>}
    {loading ? <div className="empty"><LoaderCircle className="wb-spin" size={18} /> 正在读取评测事实</div> : <>
      {tab === 'setup' && <EvaluationSetupPanel projectId={projectId} onSaved={(source) => { setFacts((old) => ({ ...old, source })); setTab('cases'); }} />}
      {tab === 'cases' && <CasesPanel projectId={projectId} cases={official} suggestedCount={facts.cases.length - official.length} sourceReady={Boolean(facts.source)} onChanged={(cases) => setFacts((old) => ({ ...old, cases }))} onOpenSetup={() => setTab('setup')} onStarted={(roundId) => { setNotice('正式评测已创建，只会运行这一轮；进度和停止入口已显示。'); setTab('runs'); void loadFacts(); void openRound(roundId); }} />}
      {tab === 'runs' && <RunsPanel projectId={projectId} rounds={facts.rounds} current={round} onOpen={(id) => void openRound(id)} onChanged={() => { void loadFacts(); if (round?.round?.id) void openRound(round.round.id); }} />}
      {followupTab && !terminalRound && <div className="wb-eval-empty">请先选择一个已结束的评测轮次 <button className="btn btn-sm" onClick={() => setTab('runs')}>查看结果</button></div>}
      {followupTab && terminalRound && currentFollowup.loading && <div className="empty"><LoaderCircle className="wb-spin" size={18} /> 正在读取评测后续信息</div>}
      {followupTab && terminalRound && currentFollowup.error && <div className="wb-eval-alert stale">{currentFollowup.error}</div>}
      {tab === 'diagnose' && terminalRound && !currentFollowup.loading && !currentFollowup.error && <DiagnosisPanel projectId={projectId} round={round} diagnoses={currentFollowup.diagnoses} runningTask={currentFollowup.runningOperation === 'diagnose' ? currentFollowup.runningTask : null} onTaskStarted={(task) => setFollowup((old) => ({ ...old, runningOperation: 'diagnose', runningTask: task }))} onTaskSettled={() => setFollowupRefresh((value) => value + 1)} onChanged={(diagnoses) => setFollowup((old) => ({ ...old, roundId: round.round.id, diagnoses }))} onGoRuns={() => setTab('runs')} onAdvance={() => setTab('optimize')} />}
      {tab === 'optimize' && terminalRound && !currentFollowup.loading && !currentFollowup.error && <OptimizationPanel projectId={projectId} round={round} diagnoses={currentFollowup.diagnoses} suggestions={currentFollowup.suggestions} runningOperation={['propose', 'apply'].includes(currentFollowup.runningOperation) ? currentFollowup.runningOperation : ''} runningTask={['propose', 'apply'].includes(currentFollowup.runningOperation) ? currentFollowup.runningTask : null} onTaskStarted={(operation, task) => setFollowup((old) => ({ ...old, runningOperation: operation, runningTask: task }))} onTaskSettled={() => setFollowupRefresh((value) => value + 1)} onChanged={(suggestions) => setFollowup((old) => ({ ...old, roundId: round.round.id, suggestions }))} onGoDiagnose={() => setTab('diagnose')} />}
    </>}
  </div>;
}

export function EvaluationSetupPanel({ projectId, onSaved }) {
  const packageDataSourceType = useStore(store, (state) => state.packageDataSource?.dataSourceType || 'neo4j');
  return <section className="wb-eval-section"><div className="wb-eval-section-head"><div className="wb-eval-icon"><Database size={17} /></div><div><h3>评测数据源</h3><p>确定真实问题是否需要查询业务数据</p></div></div><div className="mwr wb-eval-data-source"><DataSourcePanel projectId={projectId} saveDataSourceRequest={saveEvaluationDataSource} packageDataSourceType={packageDataSourceType} onAdvance={onSaved} /></div></section>;
}

function CasesPanel({ projectId, cases, suggestedCount, sourceReady, onChanged, onOpenSetup, onStarted }) {
  const fileRef = useRef(null);
  const [draft, setDraft] = useState({ question: '', groundTruth: '' });
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const reload = async () => { const result = await listEvaluationTestcases(projectId); onChanged(result.testcases || []); };
  const add = async () => { if (!draft.question.trim()) return; setBusy('add'); try { await addEvaluationTestcase(projectId, draft.question.trim(), draft.groundTruth.trim()); await reload(); setDraft({ question: '', groundTruth: '' }); } catch (cause) { setError(cause.message); } finally { setBusy(''); } };
  const readFile = async (file) => { if (!file) return; try { setPreview({ name: file.name, cases: parseTestcaseImport(await file.text()) }); setError(''); } catch (cause) { setPreview(null); setError(cause.message); } finally { if (fileRef.current) fileRef.current.value = ''; } };
  const runImport = async () => { setBusy('import'); try { const result = await addEvaluationTestcases(projectId, preview.cases); onChanged(result.testcases || []); setNotice(`已导入 ${result.added} 条，跳过 ${result.skipped} 条重复用例`); setPreview(null); } catch (cause) { setError(cause.message); } finally { setBusy(''); } };
  const template = () => { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([testcaseImportTemplateJson()], { type: 'application/json' })); a.download = 'evaluation-cases-template.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); };
  const begin = async () => {
    setBusy('evaluate'); setError(''); setNotice('');
    try {
      const result = await startEvaluation(projectId);
      setNotice('正式评测已创建，只会运行这一轮；进度和停止入口已显示。');
      onStarted(result.roundId);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(''); }
  };
  return <section className="wb-eval-section wide">
    <div className="wb-eval-section-head compact wb-eval-toolbar"><div><h3>真实业务用例</h3><p>{cases.length} 条参与正式评测{suggestedCount ? `，${suggestedCount} 条助手建议未计入` : ''}</p></div><div className="wb-eval-toolbar-actions">{!sourceReady && <button className="btn btn-sm" onClick={onOpenSetup}>配置数据源</button>}<button className="btn btn-sm" onClick={template}><Download size={14} /> 模板</button><button className="btn btn-sm" onClick={() => fileRef.current?.click()}><Upload size={14} /> 导入 JSON</button><input ref={fileRef} hidden type="file" accept=".json,application/json" onChange={(event) => void readFile(event.target.files?.[0])} /></div></div>
    {preview && <div className="wb-eval-import"><FileJson size={19} /><div><b>{preview.name}</b><span>{preview.cases.length} 条用例，{preview.cases.filter((item) => item.groundTruth).length} 条含参考答案</span></div><button className="btn btn-p btn-sm" disabled={busy === 'import'} onClick={() => void runImport()}>{busy === 'import' ? '导入中…' : '确认导入'}</button><button className="wb-icon-btn" title="取消" onClick={() => setPreview(null)}><X size={15} /></button></div>}
    {notice && <div className="wb-eval-alert ok"><Check size={15} />{notice}</div>}
    <div className="wb-eval-case-list">{cases.map((item, index) => <CaseEditor key={item.id} item={item} index={index} busy={Boolean(busy)} onSave={async (patch) => { setBusy('save'); try { const result = await updateEvaluationTestcase(projectId, item.id, patch); onChanged(result.testcases || []); } catch (cause) { setError(cause.message); } finally { setBusy(''); } }} onDelete={async () => { if (!window.confirm('确定删除这条真实用例吗？')) return; setBusy('delete'); try { await deleteEvaluationTestcase(projectId, item.id); await reload(); } catch (cause) { setError(cause.message); } finally { setBusy(''); } }} />)}{!cases.length && <div className="wb-eval-empty">可手工添加，也可导入旧建模工具使用的 JSON 用例文件</div>}</div>
    <div className="wb-eval-add compact"><div className="wb-eval-field-grid"><label><span>业务问题</span><textarea className="input" rows="2" value={draft.question} onChange={(e) => setDraft({ ...draft, question: e.target.value })} placeholder="实际会向知识助手提出的问题" /></label><label><span>参考答案 <small>可留空</small></span><textarea className="input" rows="2" value={draft.groundTruth} onChange={(e) => setDraft({ ...draft, groundTruth: e.target.value })} placeholder="关键结论、步骤或数值" /></label></div><button className="btn btn-sm" disabled={Boolean(busy) || !draft.question.trim()} onClick={() => void add()}><Plus size={14} /> 添加</button></div>
    {error && <div className="wb-eval-alert stale">{error}</div>}<div className="wb-eval-actions"><button className="btn btn-p" disabled={!sourceReady || !cases.length || Boolean(busy)} onClick={() => void begin()}>{busy === 'evaluate' ? <LoaderCircle className="wb-spin" size={14} /> : <Send size={14} />} 开始正式评测</button></div>
  </section>;
}

function CaseEditor({ item, index, busy, onSave, onDelete }) {
  const [editing, setEditing] = useState(false); const [question, setQuestion] = useState(item.question); const [answer, setAnswer] = useState(item.groundTruth || '');
  useEffect(() => { setQuestion(item.question); setAnswer(item.groundTruth || ''); }, [item.question, item.groundTruth]);
  return <article className="wb-eval-case"><div className="wb-eval-case-head"><b>{String(index + 1).padStart(2, '0')}</b><span className="wb-eval-case-question">{item.question}</span>{!item.groundTruth?.trim() && <span className="pill pill-wait">人工判定</span>}<button className="wb-icon-btn" title="编辑" onClick={() => setEditing(!editing)}><Pencil size={14} /></button><button className="wb-icon-btn danger" disabled={busy} title="删除" onClick={onDelete}><Trash2 size={14} /></button></div>{!editing && item.groundTruth && <p className="wb-eval-case-answer">参考：{item.groundTruth}</p>}{editing && <div className="wb-eval-case-edit"><textarea className="input" rows="2" value={question} onChange={(e) => setQuestion(e.target.value)} /><textarea className="input" rows="2" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="参考答案可留空" /><button className="btn btn-sm" disabled={busy || !question.trim()} onClick={() => { onSave({ question: question.trim(), groundTruth: answer.trim() }); setEditing(false); }}><Save size={14} /> 保存</button></div>}</article>;
}

export const evaluationScopeLabel = (scope) => scope?.kind === 'fusion_source' ? `来源回归 · ${scope.sourceName || '未命名来源'}` : '正式融合包';
export function RunsPanel({ projectId, rounds, current, onOpen, onChanged }) {
  if (!rounds.length) return <div className="wb-eval-empty">助手开始正式评测后，运行进度和结果会显示在这里</div>;
  return <section className="wb-eval-runs"><div className="wb-eval-round-list">{rounds.map((item) => { const [label, tone] = STATUS[item.status] || [item.status, 'plain']; return <button key={item.id} className={current?.round?.id === item.id ? 'on' : ''} onClick={() => onOpen(item.id)}><span><b>{formatTime(item.createdAt)}</b><small>{evaluationScopeLabel(item.scope)} · v{item.packageVersion}</small></span><span className={`pill pill-${tone}`}>{label}</span></button>; })}</div>{current && <RoundDetail projectId={projectId} value={current} onChanged={onChanged} />}</section>;
}
function RoundDetail({ projectId, value, onChanged }) {
  const { round, results = [], traces = {} } = value; const failed = results.filter((item) => item.verdict !== 'pass'); const [statusLabel, statusTone] = STATUS[round.status] || [round.status, 'plain']; const progress = round.progressTotal ? Math.round(round.progressDone / round.progressTotal * 100) : 0;
  const [stopping, setStopping] = useState(false);
  const stop = async () => { setStopping(true); try { await cancelEvaluationRun(projectId, round.id); onChanged?.(); } finally { setStopping(false); } };
  return <div className="wb-eval-round-detail"><div className="wb-eval-round-summary"><div><b>{statusLabel}</b><span>{evaluationScopeLabel(round.scope)} · 通过 {round.passCount}/{round.progressTotal || results.length}</span></div>{round.status === 'running' && <button type="button" className="btn btn-danger btn-sm" disabled={stopping} onClick={() => void stop()}><X size={14} />{stopping ? '停止中…' : '停止评测'}</button>}<span className={`pill pill-${statusTone}`}>{round.status === 'running' ? `${progress}%` : statusLabel}</span></div>{round.status === 'running' && <div className="wb-eval-progress"><span style={{ width: `${progress}%` }} /></div>}{failed.length > 0 && ['done', 'failed'].includes(round.status) && <div className="wb-eval-next"><Search size={18} /><div><b>{failed.length} 条用例未通过</b><span>失败回答和判定理由已在下方结果中列出。</span></div></div>}{round.status === 'failed' && <div className="wb-eval-alert stale"><b>本轮评测没有完成</b><span>{evaluationFailureSummary(round, results)}</span></div>}<div className="mwr wb-eval-results-shell"><EvaluationResultsList key={round.id} results={results} traces={traces} showTrace={Object.keys(traces).length > 0} /></div></div>;
}

export function evaluationFailureSummary(round, results = []) {
  const environmentErrors = results.filter((item) => item.verdict === 'error').length;
  if (environmentErrors) return `${environmentErrors} 条用例遇到运行环境错误，知识包没有因此自动修改。请恢复环境后重试本轮评测。`;
  if (round?.progressDone < round?.progressTotal) return `已完成 ${round.progressDone || 0}/${round.progressTotal || 0} 条，未完成部分没有生成评测结论。可检查数据源后重新开始。`;
  return '运行环境未能完成本轮评测。知识包和用例均保持不变，可排除环境问题后重试。';
}

function DiagnosisPanel({ projectId, round, diagnoses, runningTask, onTaskStarted, onTaskSettled, onChanged, onGoRuns, onAdvance }) {
  const [selected, setSelected] = useState(new Set()); const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [localTask, setLocalTask] = useState(null);
  const failedCount = (round?.results || []).filter((item) => item.verdict !== 'pass').length;
  const available = diagnoses.filter((item) => item.packageRepairEligible && !['resolved', 'irrelevant'].includes(item.resolutionStatus));
  const confirmed = diagnoses.filter((item) => item.resolutionStatus === 'confirmed' && item.packageOptimizable && item.packageRepairEligible);
  const diagnosisTask = runningTask || localTask;
  const diagnosing = busy === 'diagnose' || diagnosisTask?.operation === 'diagnose';
  useEffect(() => { setSelected(new Set(available.filter((item) => item.resolutionStatus !== 'confirmed').map((item) => item.id))); }, [diagnoses]);
  const diagnose = async () => {
    const task = { operation: 'diagnose', startedAt: new Date().toISOString() };
    setBusy('diagnose'); setError(''); setLocalTask(task); onTaskStarted?.(task);
    try {
      const data = await diagnoseEvaluation(projectId, round?.round?.id);
      onChanged(data.diagnoses || []);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(''); setLocalTask(null); onTaskSettled?.();
    }
  };
  const confirm = async () => { setBusy('confirm'); setError(''); try { const data = await confirmEvaluationDiagnoses(projectId, round.round.id, [...selected]); onChanged(data.diagnoses || []); } catch (cause) { setError(cause.message); } finally { setBusy(''); } };
  if (!round || !['done', 'failed'].includes(round.round.status)) return <div className="wb-eval-empty">请先选择一个已完成的评测轮次 <button className="btn btn-sm" onClick={onGoRuns}>查看结果</button></div>;
  return <section className="wb-eval-section wide"><StageHeader icon={Search} title="失败根因诊断" detail={`Agent 将读取 ${failedCount} 条失败用例、判定理由和执行 trace，不直接修改知识包。`} action={<button className="btn btn-p" disabled={Boolean(busy) || diagnosing || !failedCount} onClick={() => void diagnose()}>{diagnosing ? <LoaderCircle className="wb-spin" size={14} /> : <Bot size={14} />}{diagnosing ? '诊断中…' : diagnoses.length ? '重新诊断' : '开始诊断'}</button>} />
    {diagnosing && <DiagnosisProgress task={diagnosisTask} />}
    {error && <div className="wb-eval-alert stale">{error}</div>}
    {diagnoses.length > 0 && <div className="wb-diagnosis-list">{diagnoses.map((item) => { const selectable = available.some((entry) => entry.id === item.id) && item.resolutionStatus !== 'confirmed'; return <article className="wb-diagnosis" key={item.id}><label className={selectable ? '' : 'disabled'}><input type="checkbox" disabled={!selectable} checked={selected.has(item.id)} onChange={() => setSelected((old) => toggleSet(old, item.id))} /><span /></label><div><div className="wb-diagnosis-title"><b>{item.title}</b><span className={`pill pill-${item.repairScope === 'package' ? 'wait' : 'plain'}`}>{item.repairScope === 'package' ? '知识包' : item.repairScope}</span><span className="pill">置信度 {item.confidence}</span></div><p>{item.summary}</p>{item.evidence?.length > 0 && <details><summary>查看诊断证据</summary><ul>{item.evidence.map((entry, i) => <li key={i}>{entry}</li>)}</ul></details>}<div className="wb-diagnosis-action"><strong>建议处置</strong>{item.suggestedAction}</div></div>{item.resolutionStatus === 'confirmed' && <span className="wb-confirmed"><Check size={14} /> 已确认</span>}</article>; })}</div>}
    {diagnoses.length > 0 && <div className="wb-eval-actions sticky"><span>{confirmed.length} 项根因已确认可优化</span><button className="btn" disabled={!selected.size || Boolean(busy)} onClick={() => void confirm()}>{busy === 'confirm' ? '确认中…' : `确认修复范围（${selected.size}）`}</button><button className="btn btn-p" disabled={!confirmed.length} onClick={onAdvance}>生成优化建议 <ChevronRight size={14} /></button></div>}
  </section>;
}

function OptimizationPanel({ projectId, round, diagnoses, suggestions, runningOperation, runningTask, onTaskStarted, onTaskSettled, onChanged, onGoDiagnose }) {
  const [selected, setSelected] = useState(new Set()); const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [manual, setManual] = useState(false); const [reviewTask, setReviewTask] = useState(null); const [reviewNote, setReviewNote] = useState(''); const [localTask, setLocalTask] = useState(null); const [persistedTask, setPersistedTask] = useState(null);
  const knownPendingRef = useRef(new Set()); const selectionScopeRef = useRef('');
  const eligible = diagnoses.filter((item) => item.resolutionStatus === 'confirmed' && item.packageOptimizable && item.packageRepairEligible); const pending = suggestions.filter((item) => item.status === 'pending');
  const pendingIds = new Set(pending.map((item) => item.id)); const selectedPending = new Set([...selected].filter((id) => pendingIds.has(id)));
  useEffect(() => { setReviewTask(null); setReviewNote(''); setError(''); setNotice(''); setPersistedTask(null); }, [projectId, round?.round?.id]);
  useEffect(() => {
    const scope = `${projectId}:${round?.round?.id || ''}`;
    const pendingIds = suggestions.filter((item) => item.status === 'pending').map((item) => item.id);
    setSelected((previous) => {
      if (selectionScopeRef.current !== scope) {
        selectionScopeRef.current = scope;
        knownPendingRef.current = new Set(pendingIds);
        return new Set(pendingIds);
      }
      const reconciled = reconcileOptimizationSelection(previous, knownPendingRef.current, pendingIds);
      knownPendingRef.current = reconciled.known;
      return reconciled.selected;
    });
  }, [projectId, round?.round?.id, suggestions]);
  useEffect(() => { getEvaluationOptimizationStatus(projectId).then((data) => {
    const taskSuggestionIds = data.task?.node?.output?.suggestionIds || [];
    const suggestionIds = new Set(suggestions.map((item) => item.id));
    const relevant = taskSuggestionIds.some((id) => suggestionIds.has(id));
    if (!relevant) return;
    setPersistedTask(data.task || null);
    if (data.task?.node?.status === 'running') setError('');
    if (data.task?.node?.status === 'waiting_user' && data.task.node.output?.phase === 'expert_review') setReviewTask(data.task);
    else if (data.task?.node?.status === 'failed') setError(data.task.node.reason || data.task.node.output?.error || '优化候选生成失败');
    else if (data.task?.node?.status === 'done' && data.task.node.output?.candidateId) { setNotice('优化候选已生成。请在右侧确认区审核，通过后才会写入正式知识包。'); void actions.refreshSnapshot(); }
  }).catch((cause) => setError(cause.message || '优化任务状态读取失败')); }, [projectId, runningOperation, suggestions]);
  const reload = async () => { const data = await listEvaluationSuggestions(projectId, round.round.id); onChanged(data.suggestions || []); };
  const beginTask = (operation) => { const task = { operation, startedAt: new Date().toISOString() }; setLocalTask(task); onTaskStarted?.(operation, task); return task; };
  const settleTask = () => { setLocalTask(null); onTaskSettled?.(); };
  const propose = async () => { setBusy('propose'); setError(''); beginTask('propose'); try { const data = await proposeEvaluationOptimizations(projectId, round.round.id); onChanged(mergeOptimizationSuggestions(suggestions, data.suggestions || [])); await reload(); } catch (cause) { setError(cause.message); } finally { setBusy(''); settleTask(); } };
  const applyIds = async (ids) => { const data = await applyEvaluationSuggestions(projectId, ids); if (data.status === 'waiting_expert') { const status = await getEvaluationOptimizationStatus(projectId); setReviewTask(status.task); setNotice('Agent 修改未通过确定性校验，请补充约束后让 Agent 重试。'); } else { setReviewTask(null); setNotice('优化候选已生成。请在右侧确认区审核，通过后才会写入正式知识包。'); await actions.refreshSnapshot(); } await reload(); };
  const apply = async () => { setBusy('apply'); setError(''); setNotice(''); beginTask('apply'); try { await applyIds([...selectedPending]); } catch (cause) { setError(cause.message); } finally { setBusy(''); settleTask(); } };
  const review = async (action) => { if (!reviewTask?.node?.id) return; setBusy('review'); setError(''); let retrying = false; try { const decision = await reviewEvaluationOptimization(projectId, reviewTask.node.id, action, reviewNote); setReviewNote(''); if (action === 'retry') { retrying = true; beginTask('apply'); await applyIds(decision.suggestionIds); } else { setReviewTask(null); setNotice('已跳过本次未通过校验的建议，正式知识包未发生变化。'); await reload(); } } catch (cause) { setError(cause.message); } finally { setBusy(''); if (retrying) settleTask(); } };
  const reject = async (item) => { if (!window.confirm(`确认忽略“${item.title}”吗？该建议不会进入知识包候选。`)) return; setBusy('reject'); setError(''); try { await rejectEvaluationSuggestions(projectId, [item.id]); setSelected((old) => { const next = new Set(old); next.delete(item.id); return next; }); await reload(); } catch (cause) { setError(cause.message); } finally { setBusy(''); } };
  const activeOperation = runningOperation || localTask?.operation || '';
  const operationTask = persistedTask || runningTask || localTask;
  if (!round) return <div className="wb-eval-empty">请先完成失败诊断</div>;
  return <section className="wb-eval-section wide"><StageHeader icon={Sparkles} title="知识包优化建议" detail="Agent 只依据已确认根因生成建议；专家可选择、编辑或手工补充，再交给 Agent 修改知识包候选。" action={<button className="btn btn-p" disabled={Boolean(busy) || Boolean(activeOperation) || !eligible.length} onClick={() => void propose()}>{activeOperation === 'propose' ? <LoaderCircle className="wb-spin" size={14} /> : <Sparkles size={14} />}{pending.length ? '补充生成建议' : '生成建议'}</button>} />
    {activeOperation && operationTask && <OptimizationProgress operation={activeOperation} task={operationTask} />}
    {!eligible.length && <div className="wb-eval-alert wait">还没有已确认的知识包根因。<button className="btn btn-sm" onClick={onGoDiagnose}>返回失败诊断</button></div>}{error && <div className="wb-eval-alert stale">{error}</div>}{notice && <div className="wb-eval-alert ok"><Check size={15} />{notice}</div>}
    {reviewTask?.node?.status === 'waiting_user' && <div className="wb-opt-review"><b>确定性校验未通过</b><ul>{(reviewTask.node.output?.validationErrors || []).map((item, index) => <li key={index}>{item}</li>)}</ul><textarea className="input" rows="2" value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="补充修正约束，Agent 会带着校验错误和专家反馈重新修改" /><div><button className="btn btn-sm" disabled={Boolean(busy)} onClick={() => void review('skip')}>跳过本次建议</button><button className="btn btn-p btn-sm" disabled={Boolean(busy)} onClick={() => void review('retry')}><RefreshCw size={14} /> 带反馈重试</button></div></div>}
    <div className="wb-suggestion-bar"><div><b>待处理建议</b><span>{pending.length} 条，已选择 {selectedPending.size} 条</span></div><button className="btn btn-sm" disabled={!eligible.length} onClick={() => setManual(true)}><Plus size={14} /> 手工建议</button></div>
    {pending.map((item) => <SuggestionEditor key={item.id} item={item} checked={selected.has(item.id)} busy={Boolean(busy) || Boolean(activeOperation)} diagnoses={eligible} onToggle={() => setSelected((old) => toggleSet(old, item.id))} onReject={() => void reject(item)} onSave={async (input) => { setBusy('save'); try { await updateEvaluationSuggestion(projectId, item.id, input); await reload(); } catch (cause) { setError(cause.message); } finally { setBusy(''); } }} />)}
    {!pending.length && <div className="wb-eval-empty">确认根因后，让 Agent 生成修改建议，或由专家手工补充</div>}
    {manual && <SuggestionForm title="新增专家建议" diagnoses={eligible} onCancel={() => setManual(false)} onSave={async (input) => { setBusy('manual'); try { await addManualEvaluationSuggestion(projectId, { ...input, roundId: round.round.id }); await reload(); setManual(false); } catch (cause) { setError(cause.message); } finally { setBusy(''); } }} />}
    {pending.length > 0 && <div className="wb-eval-actions sticky"><span>Agent 将按选中建议生成可审核候选</span><button className="btn btn-p" disabled={!selectedPending.size || Boolean(busy) || Boolean(activeOperation)} onClick={() => void apply()}>{activeOperation === 'apply' ? <LoaderCircle className="wb-spin" size={14} /> : <Bot size={14} />} 生成知识包候选（{selectedPending.size}）</button></div>}
  </section>;
}

function SuggestionEditor({ item, checked, busy, diagnoses, onToggle, onReject, onSave }) {
  const [editing, setEditing] = useState(false);
  return <article className={`wb-suggestion ${checked ? 'selected' : ''}`}><label className="wb-suggestion-check"><input type="checkbox" disabled={busy} checked={checked} onChange={onToggle} /><span /></label><div className="wb-suggestion-body"><div className="wb-suggestion-title"><b>{item.title}</b><span className="pill">{KINDS.find(([key]) => key === item.kind)?.[1] || item.kind}</span><button className="wb-icon-btn" disabled={busy} title="编辑建议" aria-label="编辑建议" onClick={() => setEditing(!editing)}><Pencil size={14} /></button><button className="wb-icon-btn danger" disabled={busy} title="忽略建议" aria-label="忽略建议" onClick={onReject}><Trash2 size={14} /></button></div><p>{item.detail}</p>{item.target && <small>目标：{item.target}</small>}{editing && <SuggestionForm initial={item} diagnoses={diagnoses} onCancel={() => setEditing(false)} onSave={(input) => { onSave(input); setEditing(false); }} />}</div></article>;
}
function SuggestionForm({ title, initial = {}, diagnoses, onCancel, onSave }) {
  const [value, setValue] = useState({ title: initial.title || '', detail: initial.detail || '', kind: initial.kind || 'modify_rule', target: initial.target || '', relatedQuestion: initial.relatedQuestion || '', evidence: initial.evidence || [], generalizationPlan: initial.generalizationPlan || '', validationPlan: initial.validationPlan || '', diagnosisIds: initial.diagnosisIds?.length ? initial.diagnosisIds : diagnoses.map((item) => item.id) });
  const change = (key, next) => setValue((old) => ({ ...old, [key]: next }));
  return <div className="wb-suggestion-form">{title && <b>{title}</b>}<div className="wb-eval-field-grid"><label><span>建议标题</span><input className="input" value={value.title} onChange={(e) => change('title', e.target.value)} /></label><label><span>修改类型</span><select className="input" value={value.kind} onChange={(e) => change('kind', e.target.value)}>{KINDS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div><label><span>修改要求</span><textarea className="input" rows="3" value={value.detail} onChange={(e) => change('detail', e.target.value)} /></label><label><span>目标文件或资源 <small>可留空，由 Agent 定位</small></span><input className="input" value={value.target} onChange={(e) => change('target', e.target.value)} /></label><div className="wb-eval-field-grid"><label><span>泛化要求</span><textarea className="input" rows="2" value={value.generalizationPlan} onChange={(e) => change('generalizationPlan', e.target.value)} /></label><label><span>验证计划</span><textarea className="input" rows="2" value={value.validationPlan} onChange={(e) => change('validationPlan', e.target.value)} /></label></div><fieldset><legend>关联失败根因</legend>{diagnoses.map((item) => <label key={item.id}><input type="checkbox" checked={value.diagnosisIds.includes(item.id)} onChange={() => change('diagnosisIds', value.diagnosisIds.includes(item.id) ? value.diagnosisIds.filter((id) => id !== item.id) : [...value.diagnosisIds, item.id])} /> {item.title}</label>)}</fieldset><div className="wb-suggestion-form-actions"><button className="btn btn-sm" onClick={onCancel}>取消</button><button className="btn btn-p btn-sm" disabled={!value.title.trim() || !value.detail.trim() || !value.diagnosisIds.length} onClick={() => onSave(value)}><Save size={14} /> 保存建议</button></div></div>;
}
function TimedOperationProgress({ task, title, detail }) {
  const [now, setNow] = useState(Date.now());
  const state = task?.node?.output || task?.output || task || {};
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [state.startedAt]);
  const startedAt = Date.parse(state.startedAt || task?.startedAt || '');
  const elapsedSeconds = Number.isFinite(startedAt) ? Math.max(0, Math.floor((now - startedAt) / 1000)) : 0;
  const elapsed = elapsedSeconds < 60
    ? `${elapsedSeconds} 秒`
    : `${Math.floor(elapsedSeconds / 60)} 分 ${elapsedSeconds % 60} 秒`;
  return <div className="wb-eval-operation" role="status" aria-live="polite"><LoaderCircle className="wb-spin" size={18} /><div><b>{title}</b><span>{detail} · 已运行 {elapsed}，完成后自动显示结果</span></div></div>;
}
export function DiagnosisProgress({ task }) { return <TimedOperationProgress task={task} title="正在分析失败原因" detail="正在读取失败结果、执行轨迹和独立证据" />; }
export function OptimizationProgress({ operation, task }) { const state = task?.node?.output || task?.output || task || {}; const grouped = state.protocol === 'targeted_incremental' && state.changeSetCount ? `已将 ${state.selectedCount} 条建议合并为 ${state.changeSetCount} 个变更组，涉及 ${state.targetResourceKeys?.length || 0} 个资源${state.shardCount ? `；${state.shardOrdering === 'dependency_serial' ? '按依赖顺序生成' : '并行生成'} ${state.shardCount} 个符号补丁` : ''}` : ''; const copy = operation === 'apply' ? ['正在生成知识包候选', grouped || '正在落实所选建议并执行确定性校验'] : ['正在生成优化建议', '正在依据已确认根因生成并校验结构化建议']; return <TimedOperationProgress task={task} title={copy[0]} detail={copy[1]} />; }
export function mergeOptimizationSuggestions(current, incoming) { const merged = new Map((current || []).map((item) => [item.id, item])); for (const item of incoming || []) merged.set(item.id, item); return [...merged.values()]; }
export function reconcileOptimizationSelection(previous, known, pendingIds) { return { selected: new Set(pendingIds.filter((id) => previous.has(id) || !known.has(id))), known: new Set(pendingIds) }; }
function StageHeader({ icon: Icon, title, detail, action }) { return <div className="wb-stage-head"><div className="wb-eval-icon"><Icon size={17} /></div><div><h3>{title}</h3><p>{detail}</p></div><span className="spacer" />{action}</div>; }
function toggleSet(old, id) { const next = new Set(old); next.has(id) ? next.delete(id) : next.add(id); return next; }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? '未知时间' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
