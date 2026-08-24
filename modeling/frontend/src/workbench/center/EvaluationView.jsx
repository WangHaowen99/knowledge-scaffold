import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Database,
  LoaderCircle,
  Plus,
  RefreshCw,
  Send,
  Trash2,
} from 'lucide-react';
import DataSourcePanel from '../../DataSourcePanel.jsx';
import { actions, store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import {
  addEvaluationTestcase,
  deleteEvaluationTestcase,
  getEvaluationDataSource,
  getEvaluationRound,
  listEvaluationRounds,
  listEvaluationTestcases,
  saveEvaluationDataSource,
  updateEvaluationTestcase,
} from '../lib/api.js';

const TABS = [
  { id: 'setup', label: '准备' },
  { id: 'cases', label: '真实用例' },
  { id: 'runs', label: '运行结果' },
];
const STATUS = {
  running: ['运行中', 'wait'], done: ['已完成', 'ok'], failed: ['运行失败', 'stale'],
};
const VERDICT = {
  pass: ['通过', 'ok'], partial: ['部分通过', 'wait'], fail: ['未通过', 'stale'], error: ['运行错误', 'stale'],
};

export default function EvaluationView() {
  const projectId = useStore(store, (state) => state.projectId);
  const [tab, setTab] = useState('setup');
  const [dataSource, setDataSource] = useState(undefined);
  const [testcases, setTestcases] = useState(undefined);
  const [rounds, setRounds] = useState(undefined);
  const [round, setRound] = useState(null);
  const [error, setError] = useState('');

  const loadFacts = useCallback(async () => {
    if (!projectId) return;
    try {
      const [sourceResult, testcaseResult, roundResult] = await Promise.all([
        getEvaluationDataSource(projectId),
        listEvaluationTestcases(projectId),
        listEvaluationRounds(projectId),
      ]);
      setDataSource(sourceResult.dataSource || null);
      setTestcases(testcaseResult.testcases || []);
      setRounds(roundResult.rounds || []);
      setError('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '评测事实读取失败');
    }
  }, [projectId]);

  const openRound = useCallback(async (roundId) => {
    if (!projectId || !roundId) return;
    try {
      setRound(await getEvaluationRound(projectId, roundId));
      setError('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '评测结果读取失败');
    }
  }, [projectId]);

  useEffect(() => { void loadFacts(); }, [loadFacts]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadFacts();
      if (round?.round?.status === 'running') void openRound(round.round.id);
    }, 3_000);
    return () => window.clearInterval(timer);
  }, [loadFacts, openRound, round?.round?.id, round?.round?.status]);
  useEffect(() => {
    if (!round && rounds?.[0]) void openRound(rounds[0].id);
  }, [openRound, round, rounds]);

  const official = useMemo(
    () => (testcases || []).filter((item) => item.source === 'manual'),
    [testcases],
  );
  const suggestedCount = (testcases || []).length - official.length;
  const sourceReady = Boolean(dataSource);

  return (
    <div className="wb-eval">
      <div className="tabs wb-eval-tabs" role="tablist" aria-label="评测工作区">
        {TABS.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id}
            className={tab === item.id ? 'on' : ''} onClick={() => setTab(item.id)}>
            {item.label}
            {item.id === 'cases' && official.length > 0 && <span className="wb-eval-count">{official.length}</span>}
            {item.id === 'runs' && (rounds?.length || 0) > 0 && <span className="wb-eval-count">{rounds.length}</span>}
          </button>
        ))}
        <span className="spacer" />
        <button type="button" className="wb-eval-refresh" title="刷新评测事实" aria-label="刷新评测事实"
          onClick={() => { void loadFacts(); if (round?.round?.id) void openRound(round.round.id); }}>
          <RefreshCw size={14} />
        </button>
      </div>

      {error && <div className="wb-eval-alert stale">{error}</div>}
      {dataSource === undefined || testcases === undefined || rounds === undefined
        ? <div className="empty"><LoaderCircle className="wb-spin" size={18} /> 正在读取评测事实</div>
        : <>
          {tab === 'setup' && <EvaluationSetupPanel projectId={projectId}
            onSaved={(value) => { setDataSource(value); setTab('cases'); }} />}
          {tab === 'cases' && <CasesPanel projectId={projectId} cases={official}
            suggestedCount={suggestedCount} sourceReady={sourceReady}
            onChanged={setTestcases} onOpenSetup={() => setTab('setup')} onOpenRuns={() => setTab('runs')} />}
          {tab === 'runs' && <RunsPanel rounds={rounds} current={round}
            onOpen={(id) => { void openRound(id); }} />}
        </>}
    </div>
  );
}

export function EvaluationSetupPanel({ projectId, onSaved }) {
  return (
    <section className="wb-eval-section" aria-labelledby="eval-source-title">
      <div className="wb-eval-section-head">
        <div className="wb-eval-icon"><Database size={17} /></div>
        <div><h3 id="eval-source-title">业务数据</h3><p>确定评测问题是否需要查询外部业务数据</p></div>
      </div>
      <div className="mwr wb-eval-data-source">
        <DataSourcePanel
          projectId={projectId}
          saveDataSourceRequest={saveEvaluationDataSource}
          onAdvance={onSaved}
        />
      </div>
    </section>
  );
}

function CasesPanel({ projectId, cases, suggestedCount, sourceReady, onChanged, onOpenSetup, onOpenRuns }) {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [busy, setBusy] = useState(false);
  const [notifying, setNotifying] = useState(false);
  const [error, setError] = useState('');

  const reload = async () => {
    const result = await listEvaluationTestcases(projectId);
    onChanged(result.testcases || []);
  };
  const add = async () => {
    if (!question.trim() || busy) return;
    setBusy(true); setError('');
    try {
      await addEvaluationTestcase(projectId, question.trim(), answer.trim());
      await reload();
      setQuestion(''); setAnswer('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '真实用例保存失败');
    } finally {
      setBusy(false);
    }
  };
  const remove = async (id) => {
    if (!window.confirm('确定删除这条真实评测用例吗？')) return;
    setBusy(true); setError('');
    try { await deleteEvaluationTestcase(projectId, id); await reload(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '真实用例删除失败'); }
    finally { setBusy(false); }
  };
  const update = async (id, patch) => {
    setBusy(true); setError('');
    try { const result = await updateEvaluationTestcase(projectId, id, patch); onChanged(result.testcases || []); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '真实用例更新失败'); }
    finally { setBusy(false); }
  };
  const notify = async () => {
    setNotifying(true); setError('');
    try {
      const result = await actions.send(`我已在评测界面配置数据源并录入 ${cases.length} 条真实业务用例。请读取最新事实，主动进入正式评测并持续推进。`);
      if (!result) throw new Error(store.get().error || '消息发送失败');
      onOpenRuns();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '通知助手失败');
    } finally {
      setNotifying(false);
    }
  };

  return (
    <section className="wb-eval-section" aria-labelledby="eval-cases-title">
      <div className="wb-eval-section-head compact">
        <div><h3 id="eval-cases-title">真实业务用例</h3><p>{cases.length} 条参与正式评测{suggestedCount ? `，${suggestedCount} 条助手建议未计入` : ''}</p></div>
        {!sourceReady && <button type="button" className="btn btn-sm" onClick={onOpenSetup}>先配置数据源</button>}
      </div>
      {cases.map((item, index) => (
        <CaseEditor key={item.id} item={item} index={index} busy={busy}
          onSave={(patch) => { void update(item.id, patch); }} onDelete={() => { void remove(item.id); }} />
      ))}
      {cases.length === 0 && <div className="wb-eval-empty">还没有真实业务用例</div>}

      <div className="wb-eval-add">
        <label><span>业务问题</span><textarea className="input" rows="2" value={question}
          onChange={(event) => setQuestion(event.target.value)} placeholder="输入实际会向知识助手提问的问题" /></label>
        <label><span>参考答案 <small>可留空，评测后人工判定</small></span><textarea className="input" rows="2" value={answer}
          onChange={(event) => setAnswer(event.target.value)} placeholder="关键结论、步骤或数值" /></label>
        <button type="button" className="btn" disabled={busy || !question.trim()} onClick={() => { void add(); }}>
          {busy ? <LoaderCircle className="wb-spin" size={14} /> : <Plus size={14} />} 添加用例
        </button>
      </div>
      {error && <div className="wb-eval-alert stale">{error}</div>}
      <div className="wb-eval-actions">
        <button type="button" className="btn btn-p" disabled={!sourceReady || cases.length === 0 || notifying}
          onClick={() => { void notify(); }}>
          {notifying ? <><LoaderCircle className="wb-spin" size={14} /> 已通知，等待响应</> : <><Send size={14} /> 通知助手开始评测</>}
        </button>
      </div>
    </section>
  );
}

function CaseEditor({ item, index, busy, onSave, onDelete }) {
  const [question, setQuestion] = useState(item.question);
  const [answer, setAnswer] = useState(item.groundTruth || '');
  useEffect(() => { setQuestion(item.question); setAnswer(item.groundTruth || ''); }, [item.question, item.groundTruth]);
  const dirty = question !== item.question || answer !== (item.groundTruth || '');
  return (
    <article className="wb-eval-case">
      <div className="wb-eval-case-head">
        <b>用例 {String(index + 1).padStart(2, '0')}</b>
        {!item.groundTruth?.trim() && <span className="pill pill-wait">需人工判定</span>}
        <span className="spacer" />
        <button type="button" className="wb-icon-btn danger" disabled={busy} title="删除用例" aria-label={`删除用例 ${index + 1}`} onClick={onDelete}>
          <Trash2 size={14} />
        </button>
      </div>
      <label><span>问题</span><textarea className="input" rows="2" value={question} onChange={(event) => setQuestion(event.target.value)} /></label>
      <label><span>参考答案</span><textarea className="input" rows="2" value={answer} placeholder="留空则评测后人工判定"
        onChange={(event) => setAnswer(event.target.value)} /></label>
      {dirty && <button type="button" className="btn btn-sm" disabled={busy || !question.trim()}
        onClick={() => onSave({ question: question.trim(), groundTruth: answer.trim() })}>保存修改</button>}
    </article>
  );
}

export function evaluationScopeLabel(scope) {
  return scope?.kind === 'fusion_source'
    ? `来源回归 · ${scope.sourceName || '未命名来源'}`
    : '正式融合包';
}

export function RunsPanel({ rounds, current, onOpen }) {
  if (!rounds.length) return <div className="wb-eval-empty">助手开始正式评测后，运行进度和结果会显示在这里</div>;
  return (
    <section className="wb-eval-runs">
      <div className="wb-eval-round-list" aria-label="评测轮次">
        {rounds.map((item) => {
          const [label, tone] = STATUS[item.status] || [item.status, 'plain'];
          return <button type="button" key={item.id} className={current?.round?.id === item.id ? 'on' : ''} onClick={() => onOpen(item.id)}>
            <span><b>{formatTime(item.createdAt)}</b><small>{evaluationScopeLabel(item.scope)} · 版本 {item.packageVersion}</small></span>
            <span className={`pill pill-${tone}`}>{label}</span>
          </button>;
        })}
      </div>
      {current && <RoundDetail value={current} />}
    </section>
  );
}

function RoundDetail({ value }) {
  const { round, results = [] } = value;
  const [statusLabel, statusTone] = STATUS[round.status] || [round.status, 'plain'];
  const progress = round.progressTotal ? Math.round((round.progressDone / round.progressTotal) * 100) : 0;
  return (
    <div className="wb-eval-round-detail">
      <div className="wb-eval-round-summary">
        <div><b>{statusLabel}</b><span>{evaluationScopeLabel(round.scope)} · 通过 {round.passCount}/{round.progressTotal || results.length}</span></div>
        <span className={`pill pill-${statusTone}`}>{round.status === 'running' ? `${progress}%` : statusLabel}</span>
      </div>
      {round.status === 'running' && <div className="wb-eval-progress"><span style={{ width: `${progress}%` }} /></div>}
      {results.map((result, index) => {
        const [label, tone] = VERDICT[result.verdict] || ['待判定', 'plain'];
        return <article className="wb-eval-result" key={result.id}>
          <div><b>{index + 1}. {result.question}</b><span className={`pill pill-${tone}`}>{label}</span></div>
          {result.finalAnswer && <p><strong>回答</strong>{result.finalAnswer}</p>}
          {result.reason && <p><strong>判定</strong>{result.reason}</p>}
        </article>;
      })}
      {round.status === 'failed' && round.log && <pre className="wb-eval-log">{round.log}</pre>}
    </div>
  );
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未知时间';
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
