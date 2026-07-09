import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  getInterview, startInterview, answerInterview,
  getCompetency, proposeCompetency, selectCompetency,
  generatePackage, getPackage, getPackageFile, exportPackageUrl, getToken,
  savePackageFile, resetPackageFile,
} from './api.js';
import { BusyButton } from './ui.jsx';
import PackageCheck from './PackageCheck.jsx';

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
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <BusyButton busy={busy} busyText="agent 出题中" expect="约 30~90 秒" onClick={start}>
            ▶ 开始建模访谈
          </BusyButton>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>围绕对象划分 / 字段含义 / 关系 / 口径出题，一般 2~3 批</div>
        </div>
      )}
      {batches.filter((b) => b.answered).map((b) => <AnsweredBatch key={b.id} batch={b} />)}
      {pending && <BatchForm batch={pending} busy={busy} onSubmit={submit} />}
      {finished && !pending && batches.length > 0 && (
        <div style={{ textAlign: 'center', padding: '20px 0' }}>
          <div className="tag tag-ok" style={{ fontSize: 12.5, padding: '5px 16px' }}>✓ 访谈已完成</div>
          <div style={{ marginTop: 14 }}>
            <button className="btn btn-p" onClick={onAdvance}>进入知识包生成 →</button>
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
      {batch.questions.map((q, qi) => {
        const a = get(q.id);
        return (
          <div key={q.id} className={`iv-q ${a.skipped ? 'skipped' : ''}`}>
            <div className="iv-qh">
              <span className="iv-no">{qi + 1}</span>
              <span className="iv-qt">{q.q}</span>
              <span className="iv-tags">
                <span className="tag tag-lang">{q.topic}</span>
                {q.kind === 'multi' && <span className="tag tag-lang">多选</span>}
              </span>
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
        <BusyButton busy={busy} busyText="agent 在决定下一步" expect="约 30~90 秒" onClick={submit}>提交本批答案</BusyButton>
      </div>
    </div>
  );
}

/* ================= 领域问题（并入领域分析步骤） ================= */

export function DomainQuestionsBlock({ projectId, onCountChange }) {
  const [questions, setQuestions] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const report = (qs) => onCountChange?.(qs.filter((x) => x.selected).length);
  const load = () => getCompetency(projectId).then((d) => { setQuestions(d.questions); report(d.questions); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [projectId]);

  const propose = async () => {
    setBusy(true); setErr('');
    try { await proposeCompetency(projectId); await load(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const toggleSel = (q) => {
    setQuestions((prev) => {
      const next = prev.map((x) => (x.id === q.id ? { ...x, selected: !x.selected } : x));
      // 副作用移出 render 阶段（updater 内更新父组件状态会被 React 丢弃）；
      // 保存成功后再上报计数——父组件的节点解锁依赖服务端一致的选中数
      queueMicrotask(() => {
        selectCompetency(projectId, next.filter((x) => x.selected).map((x) => x.id))
          .then(() => report(next)).catch(() => {});
      });
      return next;
    });
  };

  if (err) return <div className="lerr">{err}</div>;
  if (!questions) return <div className="empty" style={{ padding: '14px 0' }}>加载中…</div>;
  const selectedCount = questions.filter((q) => q.selected).length;

  if (!questions.length) {
    return (
      <div style={{ padding: '6px 0' }}>
        <BusyButton busy={busy} busyText="生成候选中" expect="约 30~60 秒" className="btn" onClick={propose}>生成领域问题候选</BusyButton>
      </div>
    );
  }
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 12.5, color: 'var(--ink2)' }}>已选 <b>{selectedCount}</b> / {questions.length}</span>
        <BusyButton busy={busy} busyText="重新生成中" className="btn" style={{ marginLeft: 'auto', padding: '4px 12px', fontSize: 12 }} onClick={propose}>重新生成候选</BusyButton>
      </div>
      {questions.map((q) => (
        <button key={q.id} className={`cq-item ${q.selected ? 'on' : ''}`} onClick={() => toggleSel(q)}>
          <span className="cq-check">{q.selected ? '☑' : '☐'}</span>
          <span>{q.question}</span>
        </button>
      ))}
    </div>
  );
}

/* ================= 节点 5 · 知识包生成 ================= */

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
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <BusyButton busy={busy} busyText="生成中" expect="规格较大时可达数分钟" onClick={gen}>▶ 生成知识包</BusyButton>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>基于数据字典、访谈结论与选中的领域问题产出标准包结构</div>
        </div>
      ) : (
        <div>
          <div style={{ display: 'flex', gap: 24, marginBottom: 18 }}>
            <Stat v={result.version} k="版本" />
            <Stat v={result.objectCount} k="业务对象" />
            <Stat v={result.operationCount} k="操作" />
            <Stat v={`${result.coverage.covered.length}/${result.coverage.covered.length + result.coverage.uncovered.length}`} k="领域问题覆盖" />
          </div>
          <h4 style={{ fontSize: 13.5, color: 'var(--ink2)', marginBottom: 8 }}>覆盖明细</h4>
          {result.coverage.covered.map((c) => (
            <div key={c.question} className="cov-row ok">✓ {c.question} <span className="cov-op">→ {c.operation}</span></div>
          ))}
          {result.coverage.uncovered.map((q) => (
            <div key={q} className="cov-row bad">✗ 未覆盖：{q}</div>
          ))}
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 16 }}>
            <BusyButton busy={busy} busyText="重新生成中" expect="数分钟" className="btn" onClick={gen}>重新生成</BusyButton>
            <button className="btn btn-p" onClick={onAdvance}>进入知识包完善 →</button>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ v, k }) {
  return <div><div style={{ fontSize: 22, fontWeight: 750 }}>{v}</div><div style={{ fontSize: 12, color: 'var(--muted)' }}>{k}</div></div>;
}

/* ================= 节点 6 · 知识包完善（查看 / 编辑 / 导出；右栏为修缮对话） ================= */

export function ReviewPanel({ projectId }) {
  const [pkg, setPkg] = useState(null);
  const [err, setErr] = useState('');
  const [path, setPath] = useState(null);
  const [file, setFile] = useState(null); // { content, overridden, rendered }
  const [view, setView] = useState('render'); // render | source | edit
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');

  const loadPkg = () => getPackage(projectId).then((d) => {
    setPkg(d);
    setPath((p) => (p && d.files.includes(p) ? p : (d.files.includes('index.md') ? 'index.md' : d.files[0])));
  }).catch((e) => setErr(e.message));
  useEffect(() => { loadPkg(); }, [projectId]);
  // 右栏修缮对话生成新版本后自动刷新文件树与当前文件
  useEffect(() => {
    const h = () => { loadPkg(); if (path) getPackageFile(projectId, path).then((d) => { setFile(d); setDraft(d.content); }).catch(() => {}); };
    window.addEventListener('mw-package-updated', h);
    return () => window.removeEventListener('mw-package-updated', h);
  }, [projectId, path]);

  const loadFile = () => {
    if (!path) return;
    getPackageFile(projectId, path).then((d) => { setFile(d); setDraft(d.content); }).catch((e) => setErr(e.message));
  };
  useEffect(() => { loadFile(); setView('render'); setMsg(''); }, [projectId, path]);

  const save = async () => {
    setSaving(true); setMsg('');
    try {
      await savePackageFile(projectId, path, draft);
      setMsg('✓ 修订已保存（导出与评测将使用修订稿）');
      await loadPkg(); loadFile();
    } catch (e) { setMsg(`保存失败：${e.message}`); } finally { setSaving(false); }
  };
  const reset = async () => {
    if (!window.confirm('恢复为系统渲染稿？你的人工修订将被删除。')) return;
    setSaving(true); setMsg('');
    try {
      await resetPackageFile(projectId, path);
      setMsg('已恢复渲染稿');
      await loadPkg(); loadFile();
    } catch (e) { setMsg(`恢复失败：${e.message}`); } finally { setSaving(false); }
  };

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
  const overriddenSet = new Set(pkg.overriddenPaths ?? []);
  const content = file?.content ?? '';
  // 体检发现的 target（对象/操作 key）→ 包内文件路径
  const openByTarget = (target) => {
    const hit = pkg.files.find((f) => f === target)
      ?? pkg.files.find((f) => f.endsWith(`/${target}.md`))
      ?? pkg.files.find((f) => f.includes(target));
    if (hit) setPath(hit);
  };

  return (
    <div>
    <PackageCheck projectId={projectId} onOpenFile={openByTarget} />
    <div className="rv">
      <div className="rv-files">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--ink2)' }}>v{pkg.version}</span>
          <button className="btn" style={{ marginLeft: 'auto', padding: '3px 10px', fontSize: 11.5 }} onClick={download}>导出 zip</button>
        </div>
        {pkg.files.map((f) => (
          <button key={f} className={`rv-file ${f === path ? 'on' : ''}`} onClick={() => setPath(f)}>
            {f}{overriddenSet.has(f) ? ' ✎' : ''}
          </button>
        ))}
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>✎ 已修订</div>
      </div>
      <div className="rv-view">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12, color: 'var(--muted)' }}>
            {path}{file?.overridden ? <span className="tag tag-draft" style={{ marginLeft: 8 }}>人工修订稿</span> : null}
          </span>
          <span className="viewtog" style={{ marginLeft: 'auto' }}>
            {isMd && <button className={view === 'render' ? 'on' : ''} onClick={() => setView('render')}>渲染</button>}
            <button className={view === 'source' ? 'on' : ''} onClick={() => setView('source')}>源码</button>
            <button className={view === 'edit' ? 'on' : ''} onClick={() => { setDraft(content); setView('edit'); }}>编辑</button>
          </span>
        </div>
        {view === 'edit' ? (
          <div>
            <textarea className="rv-editor" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} />
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
              <button className="btn btn-p" disabled={saving || draft === content} onClick={save}>{saving ? '保存中…' : '保存修订'}</button>
              {file?.overridden && <button className="btn" disabled={saving} onClick={reset}>恢复渲染稿</button>}
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>{msg || '修订对导出与评测生效，新版本生成后仍保留'}</span>
            </div>
          </div>
        ) : isMd && view === 'render'
          ? <div className="mdv"><ReactMarkdown remarkPlugins={[remarkGfm]}>{stripFrontmatter(content)}</ReactMarkdown></div>
          : <pre className="srcview">{content}</pre>}
      </div>
    </div>
    </div>
  );
}

function stripFrontmatter(md) {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(md || '');
  return m ? md.slice(m[0].length) : md;
}
