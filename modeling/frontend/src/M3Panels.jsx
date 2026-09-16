import React, { useEffect, useRef, useState } from 'react';
import {
  listTestcases, addTestcase, addTestcases, prefillTestcases, updateTestcase, deleteTestcase,
  startEvaluate, cancelEvaluate, listRounds, getRound, overrideVerdict, getDataSource,
} from './api.js';
import { AutoTextarea } from './ui.jsx';
import { DataSourceSummary } from './DataSourcePanel.jsx';
import { requireOverrideReason } from './optimization-flow.js';
import { followUpStepForRound } from './evaluation-followup.js';
import EvaluationProgress from './EvaluationProgress.jsx';
import { EvaluationResultsList } from './EvaluationResultCard.jsx';
import { summarizeEvaluationCases } from './evaluationCaseScope.js';
import { parseTestcaseImport, testcaseImportTemplateJson } from './testcase-import.js';

/* ================= 节点 9 · 评测用例录入 ================= */

export function TestcaseImportPreview({ cases, filename, busy = false, onConfirm, onCancel }) {
  const referenced = cases.filter((item) => item.groundTruth.trim()).length;
  return (
    <section className="tc-import-preview" aria-label="评测用例导入预览">
      <div className="tc-import-icon" aria-hidden="true">导</div>
      <div className="tc-import-body">
        <strong>待导入 {cases.length} 条真实用例</strong>
        <span>{filename} · {referenced} 条含参考答案 · {cases.length - referenced} 条需人工判定</span>
      </div>
      <div className="tc-import-actions">
        <button type="button" className="btn btn-p" disabled={busy} onClick={onConfirm}>{busy ? '导入中…' : '确认导入'}</button>
        <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>
      </div>
    </section>
  );
}

export function TestcaseActions({ busy = false, onImport, onDownloadTemplate }) {
  return (
    <div className="tc-toolbar">
      <button className="btn" disabled={busy} onClick={onImport}>批量导入 JSON</button>
      <button className="btn tc-template-download" disabled={busy} onClick={onDownloadTemplate}>
        下载 JSON 模板
      </button>
    </div>
  );
}

function downloadTestcaseTemplate() {
  const url = URL.createObjectURL(new Blob([testcaseImportTemplateJson()], { type: 'application/json;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = '评测用例导入模板.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function normalizeTestcasePatch(question, groundTruth) {
  return {
    question: question.trim(),
    groundTruth: groundTruth.trim(),
  };
}

export function canSaveTestcasePatch(question) {
  return Boolean(question.trim());
}

export function resolveLoadedCollectionItems(currentItems = [], resultItems = [], error = '', preserveOnError = true) {
  if (error && preserveOnError) return currentItems;
  return resultItems;
}

export async function loadTestcasesData(projectId, request = listTestcases) {
  try {
    const result = await request(projectId);
    return { testcases: result.testcases, error: '' };
  } catch (error) {
    return { testcases: [], error: error instanceof Error ? error.message : String(error) };
  }
}

export async function loadEvaluationRoundsData(projectId, request = listRounds) {
  try {
    const result = await request(projectId);
    return { rounds: result.rounds, error: '' };
  } catch (error) {
    return { rounds: [], error: error instanceof Error ? error.message : String(error) };
  }
}

export async function loadEvaluationRoundDetail(projectId, roundId, request = getRound) {
  try {
    const detail = await request(projectId, roundId);
    return { detail, error: '' };
  } catch (error) {
    return { detail: null, error: error instanceof Error ? error.message : String(error) };
  }
}

export async function pollEvaluationRound(projectId, roundId, request = getRound) {
  try {
    const detail = await request(projectId, roundId);
    return { detail, error: '', retry: detail.round.status === 'running' };
  } catch (error) {
    return { detail: null, error: error instanceof Error ? error.message : String(error), retry: true };
  }
}

export function findActiveEvaluationRound(rounds = []) {
  return rounds.find((item) => item.status === 'running') || null;
}

export function pickSelectedEvaluationRoundId(rounds = [], selectedRoundId = null) {
  if (selectedRoundId && rounds.some((item) => item.id === selectedRoundId)) return selectedRoundId;
  return rounds[0]?.id ?? null;
}

export function summarizeEvaluationRoundView(rounds = [], selectedRoundId = null) {
  const activeRound = findActiveEvaluationRound(rounds);
  const nextSelectedRoundId = pickSelectedEvaluationRoundId(rounds, selectedRoundId);
  return {
    activeRoundId: activeRound?.id ?? null,
    selectedRoundId: nextSelectedRoundId,
    running: Boolean(activeRound),
  };
}

export function upsertEvaluationRound(rounds = [], round) {
  if (!round?.id) return rounds;
  const next = rounds.some((item) => item.id === round.id)
    ? rounds.map((item) => (item.id === round.id ? { ...item, ...round } : item))
    : [...rounds, round];
  return next.slice().sort((left, right) => String(right.createdAt ?? '').localeCompare(String(left.createdAt ?? '')));
}

export function mergeEvaluationRoundDetail(detail, round) {
  if (!detail || !round || detail.round?.id !== round.id) return detail;
  return {
    ...detail,
    round: { ...detail.round, ...round },
  };
}

export async function recoverStartedEvaluationRound(projectId, roundId, rounds = [], request = getRound) {
  const result = await loadEvaluationRoundDetail(projectId, roundId, request);
  if (!result.detail) return { rounds, detail: null, error: result.error };
  return {
    rounds: upsertEvaluationRound(rounds, result.detail.round),
    detail: result.detail,
    error: result.error,
  };
}

export async function recoverStoppedEvaluationRound(
  projectId,
  stoppedRoundId,
  selectedRoundId,
  rounds = [],
  request = getRound,
) {
  if (!selectedRoundId || selectedRoundId !== stoppedRoundId) {
    return { rounds, detail: null, error: '', refreshed: false };
  }
  const recovered = await recoverStartedEvaluationRound(projectId, stoppedRoundId, rounds, request);
  return { ...recovered, refreshed: true };
}

export function TestcasesPanel({ projectId, onAdvance }) {
  const [cases, setCases] = useState([]);
  const [casesLoaded, setCasesLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [q, setQ] = useState('');
  const [gt, setGt] = useState('');
  const [importPreview, setImportPreview] = useState(null);
  const [importNotice, setImportNotice] = useState('');
  const importInputRef = useRef(null);
  const casesRef = useRef([]);

  const applyCases = (nextCases) => {
    casesRef.current = nextCases;
    setCases(nextCases);
  };

  const load = async ({ preserveOnError = true } = {}) => {
    const result = await loadTestcasesData(projectId);
    const nextCases = resolveLoadedCollectionItems(casesRef.current, result.testcases, result.error, preserveOnError);
    if (nextCases !== casesRef.current) applyCases(nextCases);
    setErr(result.error);
    setCasesLoaded(true);
    return nextCases;
  };
  useEffect(() => {
    setCasesLoaded(false);
    applyCases([]);
    void load({ preserveOnError: false });
  }, [projectId]);

  const run = async (fn) => {
    setBusy(true); setErr('');
    try { await fn(); await load(); } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const chooseImportFile = async (file) => {
    if (!file) return;
    setErr(''); setImportNotice('');
    try {
      const imported = parseTestcaseImport(await file.text());
      setImportPreview({ cases: imported, filename: file.name });
    } catch (error) {
      setImportPreview(null);
      setErr(error instanceof Error ? error.message : String(error));
    }
  };

  const confirmImport = async () => {
    if (!importPreview || busy) return;
    setBusy(true); setErr('');
    try {
      const result = await addTestcases(projectId, importPreview.cases);
      applyCases(result.testcases);
      setImportNotice(`已导入 ${result.added} 条${result.skipped ? `，跳过 ${result.skipped} 条重复问题` : ''}`);
      setImportPreview(null);
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally { setBusy(false); }
  };

  if (!casesLoaded) return <div className="empty">{err || '加载中…'}</div>;
  const summary = summarizeEvaluationCases(cases);

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      {importNotice && <div className="tc-import-notice">{importNotice}</div>}
      <input ref={importInputRef} type="file" hidden accept=".json,application/json" aria-label="批量导入真实评测用例"
        onChange={(event) => {
          void chooseImportFile(event.target.files?.[0]);
          event.target.value = '';
        }} />
      <div className="tc-toolbar-row">
        <TestcaseActions busy={busy}
          onImport={() => importInputRef.current?.click()}
          onDownloadTemplate={downloadTestcaseTemplate} />
        <button className="btn" disabled={busy} onClick={() => run(() => prefillTestcases(projectId))}>
          ⤓ 从领域问题预填
        </button>
        <span style={{ marginLeft: 'auto', fontSize: 12.5, color: 'var(--muted)', alignSelf: 'center' }}>
          真实用例 {summary.officialTotal} 条 · {summary.officialReady} 条有参考答案
          {summary.withoutReferenceTotal > 0 ? ` · ${summary.withoutReferenceTotal} 条需人工判定` : ''}
          {summary.suggestionTotal > 0 ? ` · 建议问题 ${summary.suggestionTotal} 条` : ''}
        </span>
      </div>

      {importPreview && <TestcaseImportPreview
        cases={importPreview.cases}
        filename={importPreview.filename}
        busy={busy}
        onConfirm={() => { void confirmImport(); }}
        onCancel={() => setImportPreview(null)}
      />}

      <div className="tc-section-title">正式评测用例 <span>由用户提供的真实场景；参考答案可选，缺少时需人工判定结果</span></div>
      {summary.official.map((c, index) => (
        <CaseRow key={c.id} c={c} index={index} busy={busy}
          onSave={(patch) => run(() => updateTestcase(projectId, c.id, patch))}
          onDelete={() => run(() => deleteTestcase(projectId, c.id))} />
      ))}
      {summary.official.length === 0 && <div className="empty">请在下方录入真实场景用例。</div>}

      {summary.suggestions.length > 0 && <>
        <div className="tc-section-title is-suggestion">建议问题 <span>用于启发和场景推演，不计入正式评测</span></div>
        {summary.suggestions.map((c, index) => (
          <CaseRow key={c.id} c={c} index={index} busy={busy}
            onSave={(patch) => run(() => updateTestcase(projectId, c.id, patch))}
            onDelete={() => run(() => deleteTestcase(projectId, c.id))} />
        ))}
      </>}

      <div className="tc-add">
        <div className="field">
          <label>真实场景问题</label>
          <AutoTextarea value={q} onChange={(e) => setQ(e.target.value)} placeholder="如：客户 A 适合哪些产品？" />
        </div>
        <div className="field">
          <label>标准答案</label>
          <AutoTextarea value={gt} onChange={(e) => setGt(e.target.value)} placeholder="关键结论或数值" />
        </div>
        <button className="btn btn-p" disabled={busy || !q.trim()}
          onClick={() => run(async () => { await addTestcase(projectId, q.trim(), gt.trim()); setQ(''); setGt(''); })}>
          添加
        </button>
      </div>

      {summary.canRun && (
        <div style={{ textAlign: 'center', marginTop: 18 }}>
          <button className="btn btn-p" onClick={onAdvance}>运行正式评测（{summary.officialTotal}）</button>
        </div>
      )}
      {summary.invalidOfficialQuestionTotal > 0 && (
        <div className="tc-scope-warning">有 {summary.invalidOfficialQuestionTotal} 条真实用例问题为空；请先修正后再运行正式评测。</div>
      )}
      {summary.withoutReferenceTotal > 0 && (
        <div className="tc-scope-warning">有 {summary.withoutReferenceTotal} 条真实用例没有参考答案；系统仍会运行这些用例，结果需要人工查看回答与工具轨迹后判定。</div>
      )}
    </div>
  );
}

export function CaseRow({ c, index = 0, busy, onSave, onDelete }) {
  const [question, setQuestion] = useState(c.question);
  const [groundTruth, setGroundTruth] = useState(c.groundTruth);
  useEffect(() => { setQuestion(c.question); setGroundTruth(c.groundTruth); }, [c.question, c.groundTruth]);
  const normalized = normalizeTestcasePatch(question, groundTruth);
  const canSave = canSaveTestcasePatch(question);
  const dirty = question !== c.question || groundTruth !== c.groundTruth;

  return (
    <div className="tc-row">
      <div className="tc-head">
        <span className="tc-index">用例 {String(index + 1).padStart(2, '0')}</span>
        {c.source === 'package' && <span className="tag tag-lang">建议问题（不参与正式评测）</span>}
        {c.source === 'competency' && <span className="tag tag-lang">历史建议（不参与正式评测）</span>}
        {!c.groundTruth.trim() && <span className="tag tag-draft">需人工判定</span>}
        <button className="tc-del" title="删除" disabled={busy}
          onClick={() => { if (window.confirm('确定删除这条评测用例吗？')) onDelete(); }}>✕</button>
      </div>
      <label className="tc-content-block tc-question-block">
        <span className="tc-content-label"><b>问题</b><small>用于发起真实场景评测</small></span>
        <AutoTextarea className="tc-q" value={question} onChange={(e) => setQuestion(e.target.value)} />
      </label>
      <label className="tc-content-block tc-answer-block">
        <span className="tc-content-label"><b>参考答案</b><small>可选，用于辅助判定评测结果</small></span>
        <AutoTextarea className="tc-gt" value={groundTruth} placeholder={c.source === 'manual' ? '可选；不填写时评测完成后由人工判定' : '可选的建议答案'}
          onChange={(e) => setGroundTruth(e.target.value)} />
      </label>
      {dirty && !canSave && <div className="lerr" style={{ marginBottom: 8 }}>问题不能为空</div>}
      {dirty && (
        <button className="btn" style={{ padding: '4px 12px', fontSize: 12 }} disabled={busy || !canSave}
          onClick={() => onSave(normalized)}>保存</button>
      )}
    </div>
  );
}

export function EvaluationRoundChips({ rounds, selectedRoundId, busy = false, onSelect }) {
  if (!rounds.length) return null;
  return (
    <div className="ev-rounds">
      {rounds.map((r) => (
        <button key={r.id}
          className={`ev-round-chip ${selectedRoundId === r.id ? 'on' : ''}`}
          disabled={busy}
          onClick={() => onSelect(r.id)}>
          v{r.packageVersion} · {fmtTime(r.createdAt)}
          {(() => { if (r.status === 'running') return ' ⏳'; return r.status === 'failed' ? ' ✕' : ` ${r.passCount}/${r.progressTotal}`; })()}
        </button>
      ))}
    </div>
  );
}

/* ================= 节点 10 · 评测运行 ================= */

export function EvaluatePanel({ projectId, onProjectChanged, onRoundFinished }) {
  const [rounds, setRounds] = useState([]);
  const [roundsLoaded, setRoundsLoaded] = useState(false);
  const [selectedRoundId, setSelectedRoundId] = useState(null);
  const [current, setCurrent] = useState(null); // 当前选中的评测轮次
  const [actionError, setActionError] = useState('');
  const [dataSourceError, setDataSourceError] = useState('');
  const [roundsError, setRoundsError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [busy, setBusy] = useState(false);
  const [dataSource, setDataSource] = useState(undefined);
  const pollRef = useRef(null);
  const notifiedRoundsRef = useRef(new Set());
  const selectedRoundIdRef = useRef(null);
  const roundsRef = useRef([]);

  const selectRound = (roundId) => {
    const nextRoundId = roundId || null;
    selectedRoundIdRef.current = nextRoundId;
    setSelectedRoundId(nextRoundId);
  };

  const applyRounds = (nextRounds) => {
    roundsRef.current = nextRounds;
    setRounds(nextRounds);
  };

  const syncRounds = (nextRounds, preferredSelectedRoundId = null) => {
    applyRounds(nextRounds);
    const view = summarizeEvaluationRoundView(nextRounds, preferredSelectedRoundId ?? selectedRoundIdRef.current);
    if (view.selectedRoundId !== selectedRoundIdRef.current) selectRound(view.selectedRoundId);
    if (!view.selectedRoundId) setCurrent(null);
    return view;
  };

  const loadRounds = async (preferredSelectedRoundId = null, { preserveOnError = true } = {}) => {
    const result = await loadEvaluationRoundsData(projectId);
    const nextRounds = resolveLoadedCollectionItems(roundsRef.current, result.rounds, result.error, preserveOnError);
    if (nextRounds !== roundsRef.current || !preserveOnError || !result.error) syncRounds(nextRounds, preferredSelectedRoundId);
    setRoundsError(result.error);
    setRoundsLoaded(true);
    return nextRounds;
  };

  const finishRound = async (round) => {
    if (notifiedRoundsRef.current.has(round.id)) return;
    notifiedRoundsRef.current.add(round.id);
    await loadRounds();
    await onProjectChanged?.();
    onRoundFinished?.(round, followUpStepForRound(round));
  };

  const openRound = async (roundId) => {
    if (!roundId) return;
    if (roundId !== selectedRoundIdRef.current) {
      setCurrent(null);
      selectRound(roundId);
      return;
    }
    const result = await loadEvaluationRoundDetail(projectId, roundId);
    if (selectedRoundIdRef.current !== roundId) return;
    setCurrent(result.detail);
    setDetailError(result.error);
  };

  const schedulePoll = (roundId) => {
    clearTimeout(pollRef.current);
    pollRef.current = setTimeout(async () => {
      const result = await pollEvaluationRound(projectId, roundId);
      if (!result.detail) {
        setDetailError(result.error);
        if (result.retry) schedulePoll(roundId);
        return;
      }
      setDetailError('');
      syncRounds(upsertEvaluationRound(roundsRef.current, result.detail.round), selectedRoundIdRef.current);
      if (selectedRoundIdRef.current === result.detail.round.id) setCurrent(result.detail);
      if (result.retry) schedulePoll(roundId);
      else await finishRound(result.detail.round);
    }, 2000);
  };

  useEffect(() => {
    clearTimeout(pollRef.current);
    setRoundsLoaded(false);
    applyRounds([]);
    selectRound(null);
    setCurrent(null);
    setDataSource(undefined);
    setActionError('');
    setDataSourceError('');
    setRoundsError('');
    setDetailError('');
    getDataSource(projectId)
      .then((result) => { setDataSource(result.dataSource || null); setDataSourceError(''); })
      .catch((e) => { setDataSource(null); setDataSourceError(e.message); });
    void loadRounds(null, { preserveOnError: false });
    return () => clearTimeout(pollRef.current);
  }, [projectId]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedRoundId) {
      setCurrent(null);
      return undefined;
    }
    setCurrent((prev) => (prev?.round.id === selectedRoundId ? prev : null));
    void loadEvaluationRoundDetail(projectId, selectedRoundId).then((result) => {
      if (cancelled || selectedRoundIdRef.current !== selectedRoundId) return;
      setCurrent(result.detail);
      setDetailError(result.error);
    });
    return () => { cancelled = true; };
  }, [projectId, selectedRoundId]);

  const roundView = summarizeEvaluationRoundView(rounds, selectedRoundId);
  const activeRoundId = roundView.activeRoundId;

  useEffect(() => {
    clearTimeout(pollRef.current);
    if (!activeRoundId) return undefined;
    schedulePoll(activeRoundId);
    return () => clearTimeout(pollRef.current);
  }, [projectId, activeRoundId]);

  const start = async () => {
    setBusy(true); setActionError('');
    try {
      const { roundId } = await startEvaluate(projectId);
      await onProjectChanged?.();
      const rounds = await loadRounds(roundId);
      if (!rounds.some((item) => item.id === roundId)) {
        const recovered = await recoverStartedEvaluationRound(projectId, roundId, rounds);
        if (recovered.detail) {
          syncRounds(recovered.rounds, roundId);
          setCurrent(recovered.detail);
          setDetailError('');
        } else if (recovered.error) {
          setDetailError(recovered.error);
        }
      }
    } catch (e) { setActionError(e.message); } finally { setBusy(false); }
  };

  const stop = async () => {
    const roundId = activeRoundId;
    if (!roundId || busy) return;
    if (!window.confirm('确定停止当前评测吗？已完成结果会保留，剩余用例不会继续运行。')) return;
    setBusy(true); setActionError('');
    try {
      clearTimeout(pollRef.current);
      const { round: stoppedRound } = await cancelEvaluate(projectId, roundId);
      syncRounds(upsertEvaluationRound(roundsRef.current, stoppedRound), selectedRoundIdRef.current);
      const preferredSelectedRoundId = selectedRoundIdRef.current;
      const rounds = await loadRounds(preferredSelectedRoundId);
      const selectedRoundId = selectedRoundIdRef.current;
      const recovered = await recoverStoppedEvaluationRound(projectId, roundId, selectedRoundId, rounds);
      if (recovered.detail) {
        syncRounds(recovered.rounds, selectedRoundId);
        setCurrent(recovered.detail);
        setDetailError('');
      } else if (recovered.refreshed) {
        const stoppedRound = recovered.rounds.find((item) => item.id === roundId);
        setCurrent((prev) => {
          if (selectedRoundIdRef.current !== roundId) return prev;
          if (stoppedRound) return mergeEvaluationRoundDetail(prev, stoppedRound);
          return prev?.round.id === roundId ? null : prev;
        });
        setDetailError(recovered.error);
      }
      await onProjectChanged?.();
    } catch (e) { setActionError(e.message); } finally { setBusy(false); }
  };

  if (!roundsLoaded) return <div className="empty">{roundsError || '加载中…'}</div>;
  const running = roundView.running;
  const panelErrors = [...new Set([actionError, roundsError, detailError].filter(Boolean))];

  return (
    <div>
      {panelErrors.map((message) => <div key={message} className="lerr">{message}</div>)}

      {/* 数据源只读摘要与启动区 */}
      <DataSourceSummary dataSource={dataSource} error={dataSourceError} />
      <div style={{ marginTop: 10 }}>
        <button className="btn btn-p" disabled={busy || running || !dataSource} onClick={start}>
          {running ? '评测中…' : '开始评测'}
        </button>
        {running && (
          <button className="btn" style={{ marginLeft: 8 }} disabled={busy} onClick={stop}>
            {busy ? '停止中…' : '停止本轮'}
          </button>
        )}
        {dataSource === null && !dataSourceError && (
          <span style={{ color: 'var(--crit)', fontSize: 12, marginLeft: 10 }}>请先配置数据源</span>
        )}
      </div>

      {/* 轮次历史 */}
      <EvaluationRoundChips rounds={rounds} selectedRoundId={roundView.selectedRoundId} busy={busy} onSelect={openRound} />

      {/* 当前轮次详情 */}
      {current && <RoundDetail key={current.round.id} projectId={projectId} data={current} history={rounds}
        onChanged={() => openRound(current.round.id)} />}
    </div>
  );
}

function RoundDetail({ projectId, data, history, onChanged }) {
  const { round, results, traces = {} } = data;
  const pct = round.progressTotal ? Math.round((round.progressDone / round.progressTotal) * 100) : 0;
  const [logOpen, setLogOpen] = useState(false);

  return (
    <div style={{ marginTop: 18 }}>
      <div className="ev-status">
        <span className={`tag ${(() => { if (round.status === 'done') return 'tag-ok'; return round.status === 'failed' ? 'tag-err' : 'tag-draft'; })()}`}>
          {{ running: '运行中', done: '已完成', failed: '失败' }[round.status]}
        </span>
        <span style={{ fontSize: 13, fontWeight: 650 }}>
          包 v{round.packageVersion} · 通过 {round.passCount}/{round.progressTotal}
        </span>
        <button className="ev-log-btn" onClick={() => setLogOpen((v) => !v)}>{logOpen ? '收起日志' : '查看日志'}</button>
      </div>
      {round.status === 'running'
        ? <EvaluationProgress round={round} history={history} />
        : <div className="ev-bar"><div className="ev-bar-in" style={{ width: `${pct}%` }} /></div>}
      {logOpen && <pre className="ev-log">{round.log || '（暂无日志）'}</pre>}

      <EvaluationResultsList
        results={results}
        traces={traces}
        onOverride={(result, verdict, reason) => overrideVerdict(projectId, result.id, verdict, reason).then(onChanged)}
      />
      {round.status === 'running' && results.length < round.progressTotal && (
        <div className="empty" style={{ padding: '16px 0' }}>剩余 {round.progressTotal - results.length} 条用例排队中…</div>
      )}
    </div>
  );
}

function fmtTime(iso) {
  try { return new Date(iso).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
  catch { return iso; }
}
