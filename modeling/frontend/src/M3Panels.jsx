import { useEffect, useRef, useState } from 'react';
import {
  listTestcases, addTestcase, prefillTestcases, updateTestcase, deleteTestcase,
  startEvaluate, listRounds, getRound, overrideVerdict, getSettings,
} from './api.js';
import { AutoTextarea } from './ui.jsx';

/* ================= 节点 8 · 评测用例录入 ================= */

export function TestcasesPanel({ projectId, onAdvance }) {
  const [cases, setCases] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [q, setQ] = useState('');
  const [gt, setGt] = useState('');

  const load = () => listTestcases(projectId).then((d) => setCases(d.testcases)).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [projectId]);

  const run = async (fn) => {
    setBusy(true); setErr('');
    try { await fn(); await load(); } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (!cases) return <div className="empty">加载中…</div>;
  const ready = cases.filter((c) => c.groundTruth.trim()).length;

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
        <button className="btn" disabled={busy} onClick={() => run(() => prefillTestcases(projectId))}>
          ⤓ 从领域问题预填
        </button>
        <span style={{ marginLeft: 'auto', fontSize: 12.5, color: 'var(--muted)', alignSelf: 'center' }}>
          共 {cases.length} 条 · 已填标准答案 {ready} 条
        </span>
      </div>

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

/* ================= 节点 9 · 评测运行 ================= */

export function EvaluatePanel({ projectId }) {
  const [rounds, setRounds] = useState(null);
  const [current, setCurrent] = useState(null); // { round, results }
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [bindOn, setBindOn] = useState(false);
  const [bind, setBind] = useState({ provider: 'nl2sql_template', url: '', token: '', scene: '' });
  const pollRef = useRef(null);

  // 配置中心的评测数据源默认值自动预填（配了 url 就默认启用）
  useEffect(() => {
    getSettings().then((d) => {
      const b = d.evalBinding || {};
      if (b.url) { setBind({ provider: b.provider || 'nl2sql_template', url: b.url, token: b.token || '', scene: b.scene || '' }); setBindOn(true); }
    }).catch(() => {});
  }, [projectId]);

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
    loadRounds().then((rs) => { if (rs[0]) openRound(rs[0].id); });
    return () => clearTimeout(pollRef.current);
  }, [projectId]);

  const start = async () => {
    setBusy(true); setErr('');
    try {
      let binding;
      if (bindOn) {
        if (!bind.url) throw new Error('请填写数据服务 URL（或在配置中心配好默认值）');
        binding = { provider: bind.provider || 'nl2sql_template', config: { url: bind.url, token: bind.token, scene: bind.scene } };
      }
      const { roundId } = await startEvaluate(projectId, binding);
      await loadRounds();
      openRound(roundId);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (!rounds) return <div className="empty">加载中…</div>;
  const running = current?.round.status === 'running';

  return (
    <div>
      {err && <div className="lerr">{err}</div>}

      {/* 启动区 */}
      <div className="ev-launch">
        <label className="ev-bind-toggle">
          <input type="checkbox" checked={bindOn} onChange={(e) => setBindOn(e.target.checked)} />
          配置评测数据源<span style={{ color: 'var(--muted)' }}>（查询类用例需要）</span>
        </label>
        {bindOn && (
          <div style={{ marginTop: 10, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 14px' }}>
            <div className="field">
              <label>provider</label>
              <input value={bind.provider} onChange={(e) => setBind((b) => ({ ...b, provider: e.target.value }))} placeholder="nl2sql_template" />
            </div>
            <div className="field">
              <label>数据服务 URL</label>
              <input value={bind.url} onChange={(e) => setBind((b) => ({ ...b, url: e.target.value }))} placeholder="http://localhost:5600" />
            </div>
            <div className="field">
              <label>token</label>
              <input value={bind.token} onChange={(e) => setBind((b) => ({ ...b, token: e.target.value }))} />
            </div>
            <div className="field">
              <label>scene</label>
              <input value={bind.scene} onChange={(e) => setBind((b) => ({ ...b, scene: e.target.value }))} placeholder="留空 = 本项目评测库" />
            </div>
          </div>
        )}
        <button className="btn btn-p" disabled={busy || running} onClick={start} style={{ marginTop: 6 }}>
          {running ? '评测运行中…' : '▶ 开始新一轮评测'}
        </button>
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
          onOverride={(verdict) => overrideVerdict(projectId, r.id, verdict).then(onChanged)} />
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
  const [cls, label] = VERDICT_META[r.verdict] ?? ['tag-lang', '—'];
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
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <span style={{ fontSize: 12, color: 'var(--muted)', alignSelf: 'center' }}>人工改判：</span>
            {['pass', 'partial', 'fail'].map((v) => (
              <button key={v} className="btn" style={{ padding: '3px 12px', fontSize: 12 }}
                disabled={r.verdict === v} onClick={() => onOverride(v)}>
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
