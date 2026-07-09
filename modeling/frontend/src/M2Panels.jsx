import { useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  getInterview, startInterview, answerInterview,
  getCompetency, proposeCompetency, selectCompetency,
  generatePackage, getPackage, getPackageFile, exportPackageUrl, getToken,
} from './api.js';

/* ================= 节点 4 · 建模访谈 ================= */

export function InterviewPanel({ projectId, onAdvance }) {
  const [batches, setBatches] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [finished, setFinished] = useState(false);

  const load = () => getInterview(projectId).then((d) => {
    setBatches(d.batches);
    if (d.batches.length && d.batches.every((b) => b.answered)) setFinished(true);
  });
  useEffect(() => { load(); }, [projectId]);

  const start = async () => {
    setBusy(true); setErr('');
    try {
      const res = await startInterview(projectId);
      if (res.finished) setFinished(true);
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const submit = async (batchId, answers) => {
    setBusy(true); setErr('');
    try {
      const res = await answerInterview(projectId, batchId, answers);
      if (res.finished) { setFinished(true); onAdvance?.(); }
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (!batches) return <div className="empty">加载中…</div>;
  const pending = batches.find((b) => !b.answered);

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      {!batches.length && (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <div style={{ fontSize: 13.5, color: 'var(--ink2)', marginBottom: 16, lineHeight: 1.8 }}>
            访谈将围绕四类决策点出题：<b>对象划分 / 字段含义 / 关系 / 口径</b>。<br />
            每批 1~5 题，答完一批 agent 决定追问或结束。
          </div>
          <button className="btn btn-p" disabled={busy} onClick={start}>
            {busy ? '出题中…' : '▶ 开始建模访谈'}
          </button>
        </div>
      )}
      {batches.filter((b) => b.answered).map((b) => <AnsweredBatch key={b.id} batch={b} />)}
      {pending && <BatchForm batch={pending} busy={busy} onSubmit={submit} />}
      {finished && !pending && batches.length > 0 && (
        <div style={{ textAlign: 'center', padding: '20px 0' }}>
          <div className="tag tag-ok" style={{ fontSize: 12.5, padding: '5px 16px' }}>✓ 访谈已完成</div>
          <div style={{ marginTop: 14 }}>
            <button className="btn btn-p" onClick={onAdvance}>进入胜任力问题 →</button>
          </div>
        </div>
      )}
    </div>
  );
}

function AnsweredBatch({ batch }) {
  const ans = Object.fromEntries((batch.answers ?? []).map((a) => [a.questionId, a]));
  return (
    <div style={{ opacity: .75, marginBottom: 14 }}>
      <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 6 }}>第 {batch.ordinal + 1} 批 · 已作答</div>
      {batch.questions.map((q) => {
        const a = ans[q.id];
        const text = a?.skipped ? '（跳过）' : [...(a?.selected ?? []), a?.other].filter(Boolean).join('；');
        return (
          <div key={q.id} className="iv-q answered">
            <div className="iv-qt">{q.q}</div>
            <div className="iv-ans">→ {text || '（未答）'}</div>
          </div>
        );
      })}
    </div>
  );
}

function BatchForm({ batch, busy, onSubmit }) {
  const [state, setState] = useState({}); // qid -> {selected:[], other:'', skipped}
  const get = (qid) => state[qid] ?? { selected: [], other: '', skipped: false };
  const set = (qid, patch) => setState((s) => ({ ...s, [qid]: { ...get(qid), ...patch, skipped: false } }));

  const toggle = (q, opt) => {
    const cur = get(q.id);
    if (q.kind === 'single') set(q.id, { selected: cur.selected[0] === opt ? [] : [opt] });
    else set(q.id, { selected: cur.selected.includes(opt) ? cur.selected.filter((x) => x !== opt) : [...cur.selected, opt] });
  };

  const submit = () => {
    const answers = batch.questions.map((q) => {
      const a = get(q.id);
      return { questionId: q.id, selected: a.selected, other: a.other || undefined, skipped: a.skipped || (!a.selected.length && !a.other) };
    });
    onSubmit(batch.id, answers);
  };

  const answeredCount = batch.questions.filter((q) => { const a = get(q.id); return a.selected.length || a.other || a.skipped; }).length;

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--ink2)' }}>第 {batch.ordinal + 1} 批访谈题</span>
        <span className="iv-dots">{batch.questions.map((q) => {
          const a = get(q.id); const done = a.selected.length || a.other || a.skipped;
          return <span key={q.id} className={done ? 'on' : ''} />;
        })}</span>
        <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>{answeredCount}/{batch.questions.length}</span>
      </div>
      {batch.questions.map((q) => {
        const a = get(q.id);
        return (
          <div key={q.id} className={`iv-q ${a.skipped ? 'skipped' : ''}`}>
            <div className="iv-qt">
              {q.q}
              <span className="tag tag-lang" style={{ marginLeft: 8 }}>{q.topic}</span>
              {q.kind === 'multi' && <span className="tag tag-lang">多选</span>}
            </div>
            {q.options.length > 0 && (
              <div className="iv-opts">
                {q.options.map((opt) => (
                  <button key={opt} className={`iv-opt ${a.selected.includes(opt) ? 'on' : ''}`} onClick={() => toggle(q, opt)}>
                    {opt}
                  </button>
                ))}
              </div>
            )}
            <div className="iv-other">
              <input placeholder={q.kind === 'text' ? '请输入你的回答…' : '其他（补充说明）…'}
                value={a.other} onChange={(e) => set(q.id, { other: e.target.value })} />
              <button className={`btn ${a.skipped ? 'btn-p' : ''}`} style={{ padding: '5px 12px', fontSize: 12 }}
                onClick={() => setState((s) => ({ ...s, [q.id]: { selected: [], other: '', skipped: !a.skipped } }))}>跳过</button>
            </div>
          </div>
        );
      })}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
        <button className="btn btn-p" disabled={busy} onClick={submit}>{busy ? '提交中…agent 在决定下一步' : '提交本批答案'}</button>
      </div>
    </div>
  );
}

/* ================= 节点 5 · 胜任力问题 ================= */

export function CompetencyPanel({ projectId, onAdvance }) {
  const [questions, setQuestions] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [draft, setDraft] = useState('');

  const load = () => getCompetency(projectId).then((d) => setQuestions(d.questions));
  useEffect(() => { load(); }, [projectId]);

  const propose = async () => {
    setBusy(true); setErr('');
    try { await proposeCompetency(projectId); await load(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const toggleSel = (q) => {
    setQuestions((prev) => {
      const next = prev.map((x) => (x.id === q.id ? { ...x, selected: !x.selected } : x));
      // fire-and-forget 保存选择（以函数式更新保证连续快速点击不丢状态）
      selectCompetency(projectId, next.filter((x) => x.selected).map((x) => x.id)).catch(() => {});
      return next;
    });
  };

  if (!questions) return <div className="empty">加载中…</div>;
  const selectedCount = questions.filter((q) => q.selected).length;

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      {!questions.length ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <div style={{ fontSize: 13.5, color: 'var(--ink2)', marginBottom: 16, lineHeight: 1.8 }}>
            胜任力问题 = <b>这个知识包建成后必须能回答的业务问题</b>。<br />
            agent 将基于数据字典与访谈结论提出候选，勾选至少 5 条进入包生成；<br />
            选中的问题会驱动 operations 生成，并自动预填评测用例。
          </div>
          <button className="btn btn-p" disabled={busy} onClick={propose}>{busy ? '生成候选中…' : '▶ 生成候选问题'}</button>
        </div>
      ) : (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
            <span style={{ fontSize: 13, color: 'var(--ink2)' }}>已选 <b>{selectedCount}</b> / {questions.length} 条（至少 5 条）</span>
            <button className="btn" style={{ marginLeft: 'auto', padding: '5px 12px', fontSize: 12 }} disabled={busy} onClick={propose}>重新生成候选</button>
          </div>
          {questions.map((q) => (
            <button key={q.id} className={`cq-item ${q.selected ? 'on' : ''}`} onClick={() => toggleSel(q)}>
              <span className="cq-check">{q.selected ? '☑' : '☐'}</span>
              <span>{q.question}</span>
            </button>
          ))}
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
            <button className="btn btn-p" disabled={selectedCount < 5} onClick={onAdvance}
              title={selectedCount < 5 ? '至少勾选 5 条' : ''}>
              进入知识包生成 →（{selectedCount}/5）
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================= 节点 6 · 知识包生成 ================= */

export function GeneratePanel({ projectId, onAdvance }) {
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const gen = async () => {
    setBusy(true); setErr('');
    try { setResult(await generatePackage(projectId)); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      {!result ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <div style={{ fontSize: 13.5, color: 'var(--ink2)', marginBottom: 16, lineHeight: 1.8 }}>
            agent 将基于数据字典、访谈结论与选中的胜任力问题产出<b>知识包规格</b>，<br />
            系统渲染为标准包结构（manifest / objects / operations / mappings），并做胜任力覆盖检查。
          </div>
          <button className="btn btn-p" disabled={busy} onClick={gen}>{busy ? '生成中…（约 1~2 分钟）' : '▶ 生成知识包'}</button>
        </div>
      ) : (
        <div>
          <div style={{ display: 'flex', gap: 24, marginBottom: 18 }}>
            <Stat v={result.version} k="版本" />
            <Stat v={result.objectCount} k="业务对象" />
            <Stat v={result.operationCount} k="操作" />
            <Stat v={`${result.coverage.covered.length}/${result.coverage.covered.length + result.coverage.uncovered.length}`} k="胜任力覆盖" />
          </div>
          <h4 style={{ fontSize: 13.5, color: 'var(--ink2)', marginBottom: 8 }}>覆盖明细</h4>
          {result.coverage.covered.map((c) => (
            <div key={c.question} className="cov-row ok">✓ {c.question} <span className="cov-op">→ {c.operation}</span></div>
          ))}
          {result.coverage.uncovered.map((q) => (
            <div key={q} className="cov-row bad">✗ 未覆盖：{q}</div>
          ))}
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 16 }}>
            <button className="btn" disabled={busy} onClick={gen}>重新生成</button>
            <button className="btn btn-p" onClick={onAdvance}>进入包评审 →</button>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ v, k }) {
  return <div><div style={{ fontSize: 22, fontWeight: 750 }}>{v}</div><div style={{ fontSize: 12, color: 'var(--muted)' }}>{k}</div></div>;
}

/* ================= 节点 7 · 包评审 ================= */

export function ReviewPanel({ projectId }) {
  const [pkg, setPkg] = useState(null);
  const [err, setErr] = useState('');
  const [path, setPath] = useState(null);
  const [content, setContent] = useState('');
  const [view, setView] = useState('render');

  useEffect(() => {
    getPackage(projectId).then((d) => { setPkg(d); setPath(d.files.includes('index.md') ? 'index.md' : d.files[0]); })
      .catch((e) => setErr(e.message));
  }, [projectId]);

  useEffect(() => {
    if (!path) return;
    getPackageFile(projectId, path).then((d) => setContent(d.content)).catch((e) => setErr(e.message));
  }, [projectId, path]);

  const download = async () => {
    const res = await fetch(exportPackageUrl(projectId), { headers: { Authorization: `Bearer ${getToken()}` } });
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]) || 'package.zip';
    a.click();
    URL.revokeObjectURL(a.href);
  };

  if (err) return <div className="empty">{err}</div>;
  if (!pkg) return <div className="empty">加载中…</div>;
  const isMd = path?.endsWith('.md');

  return (
    <div className="rv">
      <div className="rv-files">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--ink2)' }}>v{pkg.version}</span>
          <button className="btn" style={{ marginLeft: 'auto', padding: '3px 10px', fontSize: 11.5 }} onClick={download}>导出 zip</button>
        </div>
        {pkg.files.map((f) => (
          <button key={f} className={`rv-file ${f === path ? 'on' : ''}`} onClick={() => setPath(f)}>{f}</button>
        ))}
      </div>
      <div className="rv-view">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12, color: 'var(--muted)' }}>{path}</span>
          {isMd && (
            <span className="viewtog" style={{ marginLeft: 'auto' }}>
              <button className={view !== 'source' ? 'on' : ''} onClick={() => setView('render')}>渲染</button>
              <button className={view === 'source' ? 'on' : ''} onClick={() => setView('source')}>源码</button>
            </span>
          )}
        </div>
        {isMd && view === 'render'
          ? <div className="mdv"><ReactMarkdown remarkPlugins={[remarkGfm]}>{stripFrontmatter(content)}</ReactMarkdown></div>
          : <pre className="srcview">{content}</pre>}
      </div>
    </div>
  );
}

function stripFrontmatter(md) {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(md || '');
  return m ? md.slice(m[0].length) : md;
}
