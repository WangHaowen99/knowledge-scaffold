import React, { useEffect, useRef, useState } from 'react';
import {
  listTestcases, addTestcase, addTestcases, prefillTestcases, updateTestcase, deleteTestcase,
  startEvaluate, listRounds, getRound, overrideVerdict, getDataSource,
} from './api.js';
import { AutoTextarea } from './ui.jsx';
import { DataSourceSummary } from './DataSourcePanel.jsx';
import { requireOverrideReason } from './optimization-flow.js';
import { parseTestcaseImport } from './testcase-import.js';

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

export function TestcasesPanel({ projectId, onAdvance }) {
  const [cases, setCases] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [q, setQ] = useState('');
  const [gt, setGt] = useState('');
  const [importPreview, setImportPreview] = useState(null);
  const [importNotice, setImportNotice] = useState('');
  const importInputRef = useRef(null);

  const load = () => listTestcases(projectId).then((d) => setCases(d.testcases)).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [projectId]);

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
      setCases(result.testcases);
      setImportNotice(`已导入 ${result.added} 条${result.skipped ? `，跳过 ${result.skipped} 条重复问题` : ''}`);
      setImportPreview(null);
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally { setBusy(false); }
  };

  if (!cases) return <div className="empty">加载中…</div>;
  const ready = cases.filter((c) => c.groundTruth.trim()).length;

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      {importNotice && <div className="tc-import-notice">{importNotice}</div>}
      <input ref={importInputRef} type="file" hidden accept=".json,application/json" aria-label="批量导入真实评测用例"
        onChange={(event) => {
          void chooseImportFile(event.target.files?.[0]);
          event.target.value = '';
        }} />
      <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
        <button className="btn" disabled={busy} onClick={() => importInputRef.current?.click()}>
          批量导入 JSON
        </button>
        <button className="btn" disabled={busy} onClick={() => run(() => prefillTestcases(projectId))}>
          ⤓ 从领域问题预填
        </button>
        <span style={{ marginLeft: 'auto', fontSize: 12.5, color: 'var(--muted)', alignSelf: 'center' }}>
          共 {cases.length} 条 · 已填标准答案 {ready} 条
        </span>
      </div>

      {importPreview && <TestcaseImportPreview
        cases={importPreview.cases}
        filename={importPreview.filename}
        busy={busy}
        onConfirm={() => { void confirmImport(); }}
        onCancel={() => setImportPreview(null)}
      />}

      {cases.map((c) => (
        <CaseRow key={c.id} c={c} busy={busy}
          onSave={(patch) => run(() => updateTestcase(projectId, c.id, patch))}
          onDelete={() => run(() => deleteTestcase(projectId, c.id))} />
      ))}

      <div className="tc-add">
        <div className="field">
          <label>新增用例 · 问题</label>
          <AutoTextarea value={q} onChange={(e) => setQ(e.target.value)} placeholder="例：STRUCTURE_LIST 中零件 A001 共有多少行有效？" />
        </div>
        <div className="field">
          <label>标准答案</label>
          <AutoTextarea value={gt} onChange={(e) => setGt(e.target.value)} placeholder="判分依据的关键结论 / 数值" />
        </div>
        <button className="btn btn-p" disabled={busy || !q.trim()}
          onClick={() => run(async () => { await addTestcase(projectId, q.trim(), gt.trim()); setQ(''); setGt(''); })}>
          ＋ 添加用例
        </button>
      </div>

      {ready > 0 && (
        <div style={{ textAlign: 'center', marginTop: 18 }}>
          <button className="btn btn-p" onClick={onAdvance}>进入评测运行（{ready} 条就绪）→</button>
        </div>
      )}
    </div>
  );
}

function CaseRow({ c, busy, onSave, onDelete }) {
  const [question, setQuestion] = useState(c.question);
  const [groundTruth, setGroundTruth] = useState(c.groundTruth);
  useEffect(() => { setQuestion(c.question); setGroundTruth(c.groundTruth); }, [c.question, c.groundTruth]);
  const dirty = question !== c.question || groundTruth !== c.groundTruth;

  return (
    <div className="tc-row">
      <div className="tc-head">
        {c.source === 'competency' && <span className="tag tag-lang">领域问题</span>}
        {!c.groundTruth.trim() && <span className="tag tag-draft">待补答案</span>}
        <button className="tc-del" title="删除" disabled={busy} onClick={onDelete}>✕</button>
      </div>
      <AutoTextarea className="tc-q" value={question} onChange={(e) => setQuestion(e.target.value)} />
      <AutoTextarea className="tc-gt" value={groundTruth} placeholder="标准答案（留空的用例不参与评测）"
        onChange={(e) => setGroundTruth(e.target.value)} />
      {dirty && (
        <button className="btn" style={{ padding: '4px 12px', fontSize: 12 }} disabled={busy}
          onClick={() => onSave({ question, groundTruth })}>保存修改</button>
      )}
    </div>
  );
}

/* ================= 节点 10 · 评测运行 ================= */

export function EvaluatePanel({ projectId, onProjectChanged }) {
  const [rounds, setRounds] = useState(null);
  const [current, setCurrent] = useState(null); // { round, results }
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [dataSource, setDataSource] = useState(undefined);
  const pollRef = useRef(null);

  const loadRounds = () => listRounds(projectId).then((d) => {
    setRounds(d.rounds);
    return d.rounds;
  }).catch((e) => { setErr(e.message); return []; });

  const openRound = async (roundId) => {
    try {
      const d = await getRound(projectId, roundId);
      setCurrent(d);
      if (d.round.status === 'running') schedulePoll(roundId);
    } catch (e) { setErr(e.message); }
  };

  const schedulePoll = (roundId) => {
    clearTimeout(pollRef.current);
    pollRef.current = setTimeout(async () => {
      const d = await getRound(projectId, roundId).catch(() => null);
      if (!d) return;
      setCurrent(d);
      if (d.round.status === 'running') schedulePoll(roundId);
      else loadRounds();
    }, 2000);
  };

  useEffect(() => {
    getDataSource(projectId)
      .then((result) => setDataSource(result.dataSource || null))
      .catch((e) => { setDataSource(null); setErr(e.message); });
    loadRounds().then((rs) => { if (rs[0]) openRound(rs[0].id); });
    return () => clearTimeout(pollRef.current);
  }, [projectId]);

  const start = async () => {
    setBusy(true); setErr('');
    try {
      const { roundId } = await startEvaluate(projectId);
      await onProjectChanged?.();
      await loadRounds();
      openRound(roundId);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (!rounds) return <div className="empty">加载中…</div>;
  const running = current?.round.status === 'running';

  return (
    <div>
      {err && <div className="lerr">{err}</div>}

      {/* 数据源只读摘要与启动区 */}
      <DataSourceSummary dataSource={dataSource} />
      <div style={{ marginTop: 10 }}>
        <button className="btn btn-p" disabled={busy || running || !dataSource} onClick={start}>
          {running ? '评测运行中…' : '▶ 开始新一轮评测'}
        </button>
        {dataSource === null && (
          <span style={{ color: 'var(--crit)', fontSize: 12, marginLeft: 10 }}>请先完成“数据源配置”步骤</span>
        )}
      </div>

      {/* 轮次历史 */}
      {rounds.length > 0 && (
        <div className="ev-rounds">
          {rounds.map((r) => (
            <button key={r.id}
              className={`ev-round-chip ${current?.round.id === r.id ? 'on' : ''}`}
              onClick={() => openRound(r.id)}>
              v{r.packageVersion} · {fmtTime(r.createdAt)}
              {r.status === 'running' ? ' ⏳' : r.status === 'failed' ? ' ✕' : ` ${r.passCount}/${r.progressTotal}`}
            </button>
          ))}
        </div>
      )}

      {/* 当前轮次详情 */}
      {current && <RoundDetail projectId={projectId} data={current} onChanged={() => openRound(current.round.id)} />}
    </div>
  );
}

function RoundDetail({ projectId, data, onChanged }) {
  const { round, results } = data;
  const pct = round.progressTotal ? Math.round((round.progressDone / round.progressTotal) * 100) : 0;
  const [logOpen, setLogOpen] = useState(false);

  return (
    <div style={{ marginTop: 18 }}>
      <div className="ev-status">
        <span className={`tag ${round.status === 'done' ? 'tag-ok' : round.status === 'failed' ? 'tag-err' : 'tag-draft'}`}>
          {{ running: '运行中', done: '已完成', failed: '失败' }[round.status]}
        </span>
        <span style={{ fontSize: 13, fontWeight: 650 }}>
          包 v{round.packageVersion} · 通过 {round.passCount}/{round.progressTotal}
        </span>
        <button className="ev-log-btn" onClick={() => setLogOpen((v) => !v)}>{logOpen ? '收起日志' : '查看日志'}</button>
      </div>
      <div className="ev-bar"><div className="ev-bar-in" style={{ width: `${pct}%` }} /></div>
      {logOpen && <pre className="ev-log">{round.log || '（暂无日志）'}</pre>}

      {results.map((r) => (
        <ResultCard key={r.id} r={r}
          onOverride={(verdict, reason) => overrideVerdict(projectId, r.id, verdict, reason).then(onChanged)} />
      ))}
      {round.status === 'running' && results.length < round.progressTotal && (
        <div className="empty" style={{ padding: '16px 0' }}>剩余 {round.progressTotal - results.length} 条用例排队中…</div>
      )}
    </div>
  );
}

const VERDICT_META = {
  pass: ['tag-ok', '通过'], partial: ['tag-draft', '部分'], fail: ['tag-err', '失败'], error: ['tag-err', '出错'],
};

function ResultCard({ r, onOverride }) {
  const [open, setOpen] = useState(false);
  const [overrideErr, setOverrideErr] = useState('');
  const [cls, label] = VERDICT_META[r.verdict] ?? ['tag-lang', '—'];
  const submitOverride = async (verdict) => {
    const input = window.prompt('请填写人工改判理由（将记录操作审计）', '');
    if (input === null) return;
    try {
      setOverrideErr('');
      await onOverride(verdict, requireOverrideReason(input));
    } catch (error) {
      setOverrideErr(error.message);
    }
  };
  return (
    <div className="ev-result">
      <div className="ev-result-head" onClick={() => setOpen((v) => !v)}>
        <span className={`tag ${cls}`}>{label}{r.overridden ? '·改' : ''}</span>
        <span className="ev-q">{r.question}</span>
        <span style={{ color: 'var(--muted)', fontSize: 12 }}>{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="ev-result-body">
          <div className="ev-kv"><b>标准答案</b><div>{r.groundTruth}</div></div>
          <div className="ev-kv"><b>智能体回答</b><div>{r.finalAnswer || '（无回答）'}</div></div>
          {r.reason && <div className="ev-kv"><b>判分理由</b><div>{r.reason}</div></div>}
          {r.overridden && (
            <div className="ev-kv"><b>改判审计</b><div>
              原判定：{VERDICT_META[r.originalVerdict]?.[1] ?? r.originalVerdict ?? '—'}；
              理由：{r.overrideReason || '—'}；操作人：{r.overrideActor || '—'}
            </div></div>
          )}
          {overrideErr && <div className="lerr">{overrideErr}</div>}
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <span style={{ fontSize: 12, color: 'var(--muted)', alignSelf: 'center' }}>人工改判：</span>
            {['pass', 'partial', 'fail'].map((v) => (
              <button key={v} className="btn" style={{ padding: '3px 12px', fontSize: 12 }}
                disabled={r.verdict === v || r.verdict === 'error'} onClick={() => submitOverride(v)}>
                {VERDICT_META[v][1]}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function fmtTime(iso) {
  try { return new Date(iso).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
  catch { return iso; }
}
