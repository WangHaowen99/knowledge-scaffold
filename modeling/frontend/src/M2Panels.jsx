import React, { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  getInterview, startInterview, answerInterview,
  getCompetency, addCompetency, proposeCompetency, selectCompetency,
  generatePackage, getPackage, getPackageFile, exportPackageUrl, authFetch,
  requireOkResponse,
  createPackageDraft, getPackageDraft, getPackageDraftFile, savePackageDraftFile,
  revertPackageDraftFile, discardPackageDraft, publishPackageDraft,
} from './api.js';
import { BusyButton } from './ui.jsx';
import PackageCheck from './PackageCheck.jsx';
import { batchesForNode } from './workflowNodes.js';
import { PACKAGE_DATA_SOURCE_HELP, PACKAGE_DATA_SOURCE_OPTIONS } from './package-data-source.js';
import {
  isPackageDraftEditablePath,
  isPackageDraftRevisionConflict,
  latestChangedPackageDraftPath,
} from './package-draft-ui.js';

/* ================= 节点 4 · 知识萃取访谈 ================= */

export function InterviewPanel({ projectId, node, agentBusy, onAdvance, onRefresh }) {
  const [batches, setBatches] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [activeNodeId, setActiveNodeId] = useState(null);
  const [locallyFinished, setLocallyFinished] = useState(false);

  const load = () => getInterview(projectId).then((d) => setBatches(d.batches));
  useEffect(() => {
    setActiveNodeId(null);
    setLocallyFinished(false);
    load();
  }, [projectId]);
  // 领域分析重跑后，旧访谈节点会变为 stale；本地会话状态也必须随之失效。
  useEffect(() => {
    if (node?.status === 'stale') {
      setActiveNodeId(null);
      setLocallyFinished(false);
    }
  }, [node?.id, node?.status]);
  // 服务端 agent 完成出题（running → waiting_user/done）后自动刷新批次
  useEffect(() => { if (!agentBusy) load(); }, [agentBusy]);

  const start = async () => {
    setBusy(true); setErr('');
    try {
      const res = await startInterview(projectId);
      setActiveNodeId(res.nodeId);
      setLocallyFinished(res.finished);
      await Promise.all([load(), onRefresh?.()]);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const submit = async (batchId, answers) => {
    setBusy(true); setErr('');
    try {
      const res = await answerInterview(projectId, batchId, answers);
      setActiveNodeId(res.nodeId);
      setLocallyFinished(res.finished);
      await Promise.all([load(), onRefresh?.()]);
      if (res.finished) onAdvance?.();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (!batches) return <div className="empty">加载中…</div>;
  const currentNodeId = activeNodeId ?? node?.id;
  const currentBatches = batchesForNode(batches, currentNodeId);
  const needsRestart = node?.status === 'stale' && !activeNodeId;
  const finished = locallyFinished || (node?.id === currentNodeId && node?.status === 'done');
  const pending = currentBatches.find((b) => !b.answered);
  const serverThinking = agentBusy && !busy && !pending;

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      {needsRestart && (
        <div style={{ textAlign: 'center', padding: '42px 0' }}>
          <div className="lerr" style={{ textAlign: 'left', marginBottom: 18 }}>
            领域分析已更新，原访谈答案仅作为历史记录保留。请基于最新分析重新进行知识萃取访谈。
          </div>
          <BusyButton busy={busy} busyText="agent 出题中" expect="约 30~90 秒" onClick={start}>
            ▶ 重新开始知识萃取访谈
          </BusyButton>
        </div>
      )}
      {!needsRestart && <>
      {serverThinking && (
        <div className="empty" style={{ padding: '32px 0' }}>
          <span className="spinner" style={{ marginRight: 8 }} />agent 出题中（服务端执行，切换页面不影响）…
        </div>
      )}
      {!currentBatches.length && !serverThinking && !finished && (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <BusyButton busy={busy} busyText="agent 出题中" expect="约 30~90 秒" onClick={start}>
            ▶ 开始知识萃取访谈
          </BusyButton>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>围绕对象划分 / 字段含义 / 关系 / 口径出题，一般 2~3 批</div>
        </div>
      )}
      {currentBatches.filter((b) => b.answered).map((b) => (
        <AnsweredBatch key={b.id} batch={b} displayOrdinal={currentBatches.indexOf(b)} />
      ))}
      {pending && <PendingInterviewBatch batch={pending} displayOrdinal={currentBatches.indexOf(pending)}
        busy={busy} onSubmit={submit} />}
      {finished && !pending && (
        <div style={{ textAlign: 'center', padding: '20px 0' }}>
          <div className="tag tag-ok" style={{ fontSize: 12.5, padding: '5px 16px' }}>✓ 访谈已完成</div>
          <div style={{ marginTop: 14 }}>
            <button className="btn btn-p" onClick={onAdvance}>进入知识包生成 →</button>
          </div>
        </div>
      )}
      </>}
    </div>
  );
}

export function PendingInterviewBatch({ batch, displayOrdinal, busy, onSubmit }) {
  return <BatchForm key={batch.id} batch={batch} displayOrdinal={displayOrdinal} busy={busy} onSubmit={onSubmit} />;
}

function AnsweredBatch({ batch, displayOrdinal }) {
  const ans = Object.fromEntries((batch.answers ?? []).map((a) => [a.questionId, a]));
  return (
    <div style={{ opacity: .75, marginBottom: 14 }}>
      <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 6 }}>第 {displayOrdinal + 1} 批 · 已作答</div>
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

function BatchForm({ batch, displayOrdinal, busy, onSubmit }) {
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
        <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--ink2)' }}>第 {displayOrdinal + 1} 批访谈题</span>
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

export function DomainQuestionsBlock({ projectId, generating, onCountChange }) {
  const [questions, setQuestions] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [customQuestion, setCustomQuestion] = useState('');
  const [adding, setAdding] = useState(false);
  const [addErr, setAddErr] = useState('');

  const report = (qs) => onCountChange?.(qs.filter((x) => x.selected).length);
  const load = () => getCompetency(projectId).then((d) => { setQuestions(d.questions); report(d.questions); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [projectId]);
  useEffect(() => { if (!generating) load(); }, [generating]);

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

  const addCustom = async (event) => {
    event.preventDefault();
    const question = customQuestion.trim();
    if (!question) return;
    setAdding(true); setAddErr('');
    try {
      const result = await addCompetency(projectId, question);
      setQuestions(result.questions);
      report(result.questions);
      setCustomQuestion('');
    } catch (e) { setAddErr(e.message); } finally { setAdding(false); }
  };

  if (err) return <div className="lerr">{err}</div>;
  if (!questions) return <div className="empty" style={{ padding: '14px 0' }}>加载中…</div>;
  const selectedCount = questions.filter((q) => q.selected).length;
  const customForm = (
    <form className="cq-add" onSubmit={addCustom}>
      <input aria-label="自定义领域问题" maxLength={500} placeholder="输入自定义领域问题"
        value={customQuestion} onChange={(event) => setCustomQuestion(event.target.value)} />
      <BusyButton type="submit" busy={adding} busyText="添加中" className="btn"
        disabled={adding || !customQuestion.trim()}>添加并选中</BusyButton>
      {addErr && <div className="lerr cq-add-error">{addErr}</div>}
    </form>
  );

  if (!questions.length) {
    if (generating && !busy) {
      return <div>{customForm}<div style={{ padding: '6px 0', fontSize: 12.5, color: 'var(--ink2)' }}><span className="spinner" style={{ marginRight: 8 }} />候选生成中（切换页面不影响）…</div></div>;
    }
    return (
      <div style={{ padding: '6px 0' }}>
        {customForm}
        <BusyButton busy={busy} busyText="生成候选中" expect="约 30~60 秒" className="btn" onClick={propose}>生成领域问题候选</BusyButton>
      </div>
    );
  }
  return (
    <div>
      {customForm}
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 12.5, color: 'var(--ink2)' }}>已选 <b>{selectedCount}</b> / {questions.length}</span>
        <BusyButton busy={busy} busyText="重新生成中" className="btn" style={{ marginLeft: 'auto', padding: '4px 12px', fontSize: 12 }} onClick={propose}>重新生成候选</BusyButton>
      </div>
      {questions.map((q) => (
        <button key={q.id} className={`cq-item ${q.selected ? 'on' : ''}`} onClick={() => toggleSel(q)}>
          <span className="cq-check">{q.selected ? '☑' : '☐'}</span>
          <span>{q.question}</span>
          {q.source === 'custom' && <span className="tag tag-lang cq-source">自定义</span>}
        </button>
      ))}
    </div>
  );
}

/* ================= 节点 5 · 知识包生成 ================= */

const PACKAGE_DATA_SOURCE_HELP_ID = 'package-data-source-resource-help';

export function PackageDataSourceHelp() {
  return (
    <span className="pkg-source-help">
      <button type="button" className="pkg-source-help-icon" aria-label="查看数据源映射说明"
        aria-describedby={PACKAGE_DATA_SOURCE_HELP_ID}>?</button>
      <span id={PACKAGE_DATA_SOURCE_HELP_ID} className="pkg-source-tooltip" role="tooltip">
        {PACKAGE_DATA_SOURCE_HELP}
      </span>
    </span>
  );
}

export function GeneratePanel({ projectId, node, onRefresh, onAdvance }) {
  const [localResult, setLocalResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [dataSourceType, setDataSourceType] = useState(node?.output?.dataSourceType ?? '');

  // 状态从服务端节点复原：切页/回项目列表再进来，进行中与结果都不丢
  const serverRunning = node?.status === 'running';
  const serverResult = !localResult && node?.status === 'done' && node.output ? node.output : null;
  const result = localResult ?? serverResult;
  const running = busy || serverRunning;
  useEffect(() => {
    if (node?.output?.dataSourceType) setDataSourceType(node.output.dataSourceType);
  }, [node?.output?.dataSourceType]);
  // 服务端 running 超过 15 分钟视为疑似中断（进程重启等），允许重新触发
  const runStaleMin = serverRunning && node?.updatedAt ? (Date.now() - new Date(node.updatedAt).getTime()) / 60000 : 0;

  const gen = async () => {
    if (!dataSourceType) { setErr('请选择后续使用的数据源'); return; }
    setBusy(true); setErr('');
    try { setLocalResult(await generatePackage(projectId, dataSourceType)); onRefresh?.(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      <div className="field pkg-source-field">
        <div className="pkg-source-label-row">
          <label htmlFor="package-data-source">后续使用的数据源</label>
          <PackageDataSourceHelp />
        </div>
        <select id="package-data-source" value={dataSourceType} disabled={running}
          onChange={(event) => setDataSourceType(event.target.value)}>
          <option value="" disabled>请选择数据源</option>
          {PACKAGE_DATA_SOURCE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>
      {!result ? (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          {serverRunning && !busy ? (
            <div>
              <div style={{ fontSize: 13.5, color: 'var(--ink2)' }}>
                <span className="spinner" style={{ marginRight: 8 }} />生成进行中（服务端执行，切换页面不影响）…
              </div>
              {runStaleMin > 15 && (
                <div style={{ marginTop: 14 }}>
                  <div style={{ fontSize: 12, color: 'var(--crit)', marginBottom: 8 }}>已进行超过 15 分钟，可能因服务重启而中断</div>
                  <BusyButton busy={busy} busyText="生成中" className="btn" onClick={gen}>重新生成</BusyButton>
                </div>
              )}
            </div>
          ) : (
            <>
              <BusyButton busy={busy} busyText="生成中" expect="规格较大时可达数分钟，可切换页面"
                disabled={busy || !dataSourceType} onClick={gen}>▶ 生成知识包</BusyButton>
              <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>基于数据字典、访谈结论与选中的领域问题产出标准包结构</div>
            </>
          )}
        </div>
      ) : (
        <div>
          <div style={{ display: 'flex', gap: 24, marginBottom: 18 }}>
            <Stat v={result.version} k="版本" />
            <Stat v={result.objectCount ?? '—'} k="业务对象" />
            <Stat v={result.operationCount ?? '—'} k="操作" />
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
            <BusyButton busy={running} busyText="重新生成中" expect="数分钟" className="btn"
              disabled={running || !dataSourceType} onClick={gen}>重新生成</BusyButton>
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

const PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE = '知识包修缮正在进行中，暂不能人工编辑';

export function ReviewPanel({
  projectId, packageDraft, packageRepairBusy, onDraftChange, onDirtyChange, onAdvance,
}) {
  const [pkg, setPkg] = useState(null);
  const [err, setErr] = useState('');
  const [path, setPath] = useState(null);
  const [file, setFile] = useState(null);
  const [view, setView] = useState('view');
  const [editorContent, setEditorContent] = useState('');
  const [saveState, setSaveState] = useState('idle'); // idle | unsaved | saving | saved | error
  const [actionBusy, setActionBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [publishOpen, setPublishOpen] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [publishSummary, setPublishSummary] = useState('');
  const [fileReloadKey, setFileReloadKey] = useState(0);
  const [revisionConflict, setRevisionConflict] = useState(null);
  const [publishRevisionConflict, setPublishRevisionConflict] = useState(false);
  const draftRef = useRef(packageDraft);
  const pathRef = useRef(path);
  const editorContentRef = useRef(editorContent);
  const savedContentRef = useRef('');
  const savedContentHashRef = useRef('');
  const saveTimerRef = useRef(null);
  const savePromiseRef = useRef(Promise.resolve());
  const handledDraftRef = useRef(null);
  const localChangeRef = useRef(false);
  const deferredPackageRefreshRef = useRef(false);
  const repairInProgress = !!packageRepairBusy;

  useEffect(() => { draftRef.current = packageDraft; }, [packageDraft]);
  useEffect(() => { pathRef.current = path; }, [path]);
  useEffect(() => { editorContentRef.current = editorContent; }, [editorContent]);
  useEffect(() => {
    if (!repairInProgress) return;
    clearTimeout(saveTimerRef.current);
    setPublishOpen(false);
    setDiscardOpen(false);
    setView('view');
  }, [repairInProgress]);

  const notifyDraftChange = useCallback((nextDraft) => {
    draftRef.current = nextDraft;
    onDraftChange?.(nextDraft);
  }, [onDraftChange]);

  const loadPkg = useCallback(async () => {
    const data = await getPackage(projectId);
    setPkg(data);
    setPath((current) => (localChangeRef.current && current
      ? current
      : current && data.files.includes(current)
      ? current
      : (data.files.includes('index.md') ? 'index.md' : data.files[0])));
    return data;
  }, [projectId]);

  const refreshPackageView = useCallback(() => {
    loadPkg().catch((e) => setErr(e.message));
    setFileReloadKey((key) => key + 1);
  }, [loadPkg]);

  useEffect(() => {
    localChangeRef.current = false;
    deferredPackageRefreshRef.current = false;
    setPkg(null);
    setPath(null);
    setFile(null);
    setView('view');
    setErr('');
    setMsg('');
    setRevisionConflict(null);
    setPublishRevisionConflict(false);
    handledDraftRef.current = null;
    loadPkg().catch((e) => setErr(e.message));
  }, [loadPkg]);

  // 右栏修缮对话生成新版本、或人工草稿发布后，刷新文件树与当前文件。
  useEffect(() => {
    const handlePackageUpdated = () => {
      if (localChangeRef.current) {
        deferredPackageRefreshRef.current = true;
        setMsg('检测到后台更新；当前文件有未保存修改，已保留你的输入');
        return;
      }
      refreshPackageView();
    };
    window.addEventListener('mw-package-updated', handlePackageUpdated);
    return () => window.removeEventListener('mw-package-updated', handlePackageUpdated);
  }, [refreshPackageView]);

  // 已有活动草稿时优先恢复最近修改的文件；无变更的空草稿恢复到第一个可编辑详情文件。
  useEffect(() => {
    if (!packageDraft || !pkg || handledDraftRef.current === packageDraft.id) return;
    handledDraftRef.current = packageDraft.id;
    const target = latestChangedPackageDraftPath(packageDraft)
      ?? pkg.files.find(isPackageDraftEditablePath);
    if (target) {
      setPath(target);
      setView('edit');
      setMsg(`已恢复基于 v${packageDraft.baseVersion} 的未发布草稿`);
    }
  }, [packageDraft, pkg]);

  useEffect(() => {
    if (!path) return undefined;
    if (localChangeRef.current) return undefined;
    let active = true;
    setFile(null);
    const request = draftRef.current
      ? getPackageDraftFile(projectId, path).then((data) => data.file)
      : getPackageFile(projectId, path);
    request.then((nextFile) => {
      if (!active) return;
      setFile(nextFile);
      setEditorContent(nextFile.content);
      savedContentRef.current = nextFile.content;
      savedContentHashRef.current = nextFile.contentHash ?? '';
      localChangeRef.current = false;
      setRevisionConflict(null);
      setSaveState('idle');
      if (!isPackageDraftEditablePath(path)) setView('view');
    }).catch((e) => { if (active) setErr(e.message); });
    return () => { active = false; };
  }, [projectId, path, packageDraft?.id, fileReloadKey]);

  const persistDraftFile = useCallback((targetPath, content, quiet = false, expectedFile = null) => {
    const save = async () => {
      const activeDraft = draftRef.current;
      if (!activeDraft) return null;
      const expectedRevision = expectedFile?.revision ?? activeDraft.revision;
      const expectedContentHash = (expectedFile?.contentHash ?? savedContentHashRef.current) || undefined;
      const expectedDraftId = expectedFile?.draftId ?? activeDraft.id;
      setSaveState('saving');
      try {
        const result = await savePackageDraftFile(
          projectId,
          targetPath,
          content,
          expectedRevision,
          expectedContentHash,
          expectedDraftId,
        );
        notifyDraftChange(result.draft);
        if (pathRef.current === targetPath) {
          savedContentRef.current = result.file.content;
          savedContentHashRef.current = result.file.contentHash;
          setFile((current) => ({ ...(current ?? {}), ...result.file }));
          if (editorContentRef.current === content) setEditorContent(result.file.content);
        }
        if (editorContentRef.current === content) localChangeRef.current = false;
        setRevisionConflict((current) => (current?.path === targetPath ? null : current));
        setSaveState(editorContentRef.current === content ? 'saved' : 'unsaved');
        if (!quiet) setMsg('✓ 已保存到人工编辑草稿');
        return result;
      } catch (e) {
        setSaveState('error');
        if (isPackageDraftRevisionConflict(e)) {
          try {
            const latest = await getPackageDraft(projectId);
            if (!latest.draft) {
              notifyDraftChange(null);
              setRevisionConflict(null);
              setView('view');
              setFileReloadKey((key) => key + 1);
              setMsg('当前草稿已在其他页面发布或放弃，已恢复正式版本内容。');
            } else {
              const latestFile = await getPackageDraftFile(projectId, targetPath);
              const conflictScope = e.data?.conflictScope === 'draft' ? 'draft' : 'file';
              setRevisionConflict({ path: targetPath, draft: latest.draft, file: latestFile.file, scope: conflictScope });
              notifyDraftChange(latest.draft);
              setPublishOpen(false);
              setDiscardOpen(false);
              setMsg(conflictScope === 'draft'
                ? '原草稿已在其他页面发布或放弃，自动保存已暂停。请确认是否将当前输入写入新草稿。'
                : '检测到同文件并发修改，自动保存已暂停。请选择保留当前内容或加载其他页面版本。');
            }
          } catch {
            setMsg('保存冲突：无法刷新草稿状态。当前输入仍保留，请稍后重试。');
          }
        } else {
          setMsg(`保存失败：${e.message}`);
        }
        throw e;
      }
    };
    const queued = savePromiseRef.current.catch(() => {}).then(save);
    savePromiseRef.current = queued;
    return queued;
  }, [notifyDraftChange, projectId]);

  useEffect(() => {
    clearTimeout(saveTimerRef.current);
    if (repairInProgress || revisionConflict?.path === path
      || !packageDraft || view !== 'edit' || !path || !file) return undefined;
    if (editorContent === savedContentRef.current) return undefined;
    setSaveState('unsaved');
    saveTimerRef.current = setTimeout(() => {
      persistDraftFile(path, editorContent, true).catch(() => {});
    }, 800);
    return () => clearTimeout(saveTimerRef.current);
  }, [editorContent, file, packageDraft, path, persistDraftFile, repairInProgress, revisionConflict, view]);

  const hasUnsavedContent = !!packageDraft && view === 'edit'
    && editorContent !== savedContentRef.current;
  const hasPendingLocalChanges = hasUnsavedContent || saveState === 'saving' || !!revisionConflict;
  useEffect(() => {
    localChangeRef.current = hasPendingLocalChanges;
    onDirtyChange?.(hasPendingLocalChanges);
  }, [hasPendingLocalChanges, onDirtyChange]);
  useEffect(() => () => { onDirtyChange?.(false); }, [onDirtyChange]);
  useEffect(() => {
    if (hasPendingLocalChanges || !deferredPackageRefreshRef.current) return;
    deferredPackageRefreshRef.current = false;
    refreshPackageView();
  }, [hasPendingLocalChanges, refreshPackageView]);
  useEffect(() => {
    if (!hasPendingLocalChanges) return undefined;
    const warnBeforeUnload = (event) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, [hasPendingLocalChanges]);

  const flushCurrentFile = useCallback(async () => {
    clearTimeout(saveTimerRef.current);
    if (repairInProgress) {
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return false;
    }
    if (revisionConflict?.path === pathRef.current) {
      setMsg('请先处理当前文件的保存冲突');
      return false;
    }
    try {
      await savePromiseRef.current.catch(() => {});
      const activeDraft = draftRef.current;
      const currentPath = pathRef.current;
      const currentContent = editorContentRef.current;
      if (activeDraft && view === 'edit' && currentPath
        && currentContent !== savedContentRef.current) {
        await persistDraftFile(currentPath, currentContent, true);
      }
      return true;
    } catch {
      return false;
    }
  }, [persistDraftFile, repairInProgress, revisionConflict, view]);

  const selectFile = async (nextPath) => {
    if (repairInProgress) {
      setPath(nextPath);
      setView('view');
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    if (revisionConflict?.path === path) {
      setMsg('请先处理当前文件的保存冲突，再切换文件');
      return;
    }
    if (nextPath === path) {
      if (packageDraft && isPackageDraftEditablePath(nextPath)) setView('edit');
      return;
    }
    if (!(await flushCurrentFile())) return;
    setPath(nextPath);
    setView(packageDraft && isPackageDraftEditablePath(nextPath) ? 'edit' : 'view');
    setMsg('');
  };

  const startEditing = async () => {
    if (repairInProgress) {
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    if (!path || !isPackageDraftEditablePath(path)) return;
    if (packageDraft) {
      setView('edit');
      return;
    }
    setActionBusy('create');
    setMsg('');
    try {
      const result = await createPackageDraft(projectId);
      handledDraftRef.current = result.draft.id;
      notifyDraftChange(result.draft);
      const data = await getPackageDraftFile(projectId, path);
      setFile(data.file);
      setEditorContent(data.file.content);
      savedContentRef.current = data.file.content;
      savedContentHashRef.current = data.file.contentHash;
      localChangeRef.current = false;
      setRevisionConflict(null);
      setView('edit');
      setSaveState('idle');
      setMsg(result.created ? `已创建基于 v${result.draft.baseVersion} 的人工编辑草稿` : '已恢复未发布草稿');
    } catch (e) { setMsg(`无法开始编辑：${e.message}`); }
    finally { setActionBusy(''); }
  };

  const showRenderedView = async () => {
    if (repairInProgress) {
      setView('view');
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    if (await flushCurrentFile()) setView('view');
  };

  const saveNow = async () => {
    if (repairInProgress) {
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    if (revisionConflict?.path === path) {
      setMsg('请通过冲突提示选择要保留的版本');
      return;
    }
    if (editorContent === savedContentRef.current) {
      setMsg('当前文件已保存到草稿');
      return;
    }
    if (await flushCurrentFile()) setMsg('✓ 已保存到人工编辑草稿');
  };

  const loadOtherPageVersion = () => {
    const conflict = revisionConflict;
    if (!conflict || conflict.path !== path) return;
    if (!window.confirm('加载其他页面版本将丢弃当前尚未保存的输入，是否继续？')) return;
    notifyDraftChange(conflict.draft);
    setFile(conflict.file);
    setEditorContent(conflict.file.content);
    savedContentRef.current = conflict.file.content;
    savedContentHashRef.current = conflict.file.contentHash;
    localChangeRef.current = false;
    setRevisionConflict(null);
    setSaveState('idle');
    setMsg('已加载其他页面保存的最新内容');
  };

  const overwriteRevisionConflict = async () => {
    const conflict = revisionConflict;
    if (!conflict || conflict.path !== path) return;
    if (!window.confirm('确定以当前输入覆盖其他页面对该文件的修改？')) return;
    setActionBusy('conflict-overwrite');
    setMsg('');
    try {
      await persistDraftFile(path, editorContentRef.current, false, {
        revision: conflict.draft.revision,
        contentHash: conflict.file.contentHash,
        draftId: conflict.draft.id,
      });
    } catch { /* persistDraftFile 会刷新并展示最新冲突 */ }
    finally { setActionBusy(''); }
  };

  const revertDraftFile = async () => {
    if (repairInProgress) {
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    if (revisionConflict?.path === path) {
      setMsg('请先处理当前文件的保存冲突');
      return;
    }
    if (!window.confirm(`撤销「${path}」在本次草稿中的全部修改？`)) return;
    setActionBusy('revert');
    setMsg('');
    clearTimeout(saveTimerRef.current);
    try {
      await savePromiseRef.current.catch(() => {});
      const activeDraft = draftRef.current;
      if (!activeDraft) return;
      const result = await revertPackageDraftFile(projectId, path, activeDraft.revision, activeDraft.id);
      notifyDraftChange(result.draft);
      setFile(result.file);
      setEditorContent(result.file.content);
      savedContentRef.current = result.file.content;
      savedContentHashRef.current = result.file.contentHash;
      localChangeRef.current = false;
      setRevisionConflict(null);
      setSaveState('idle');
      setMsg('已撤销当前文件在草稿中的修改');
    } catch (e) { setMsg(`撤销失败：${e.message}`); }
    finally { setActionBusy(''); }
  };

  const doPublish = async () => {
    if (repairInProgress) {
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    setActionBusy('publish');
    setMsg('');
    try {
      if (!(await flushCurrentFile())) return;
      const activeDraft = draftRef.current;
      if (!activeDraft?.changedFileCount) {
        setMsg('草稿没有待发布的文件变更；如需退出编辑，请放弃草稿。');
        return;
      }
      const result = await publishPackageDraft(projectId, activeDraft.revision, publishSummary.trim(), activeDraft.id);
      notifyDraftChange(null);
      localChangeRef.current = false;
      handledDraftRef.current = null;
      setPublishOpen(false);
      setPublishSummary('');
      setView('view');
      setMsg(`✓ 已发布为正式版本 v${result.published.version}`);
      await loadPkg();
      setFileReloadKey((key) => key + 1);
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) {
      if (isPackageDraftRevisionConflict(e)) {
        setPublishOpen(false);
        setPublishRevisionConflict(true);
        setMsg('检测到有其他修改已应用到当前草稿，请刷新页面后重新发布。');
      } else {
        if (e.data?.path && pkg?.files.includes(e.data.path)) setPath(e.data.path);
        setMsg(`发布失败：${e.message}`);
      }
    } finally { setActionBusy(''); }
  };

  const doDiscard = async () => {
    if (repairInProgress) {
      setMsg(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      return;
    }
    if (revisionConflict) {
      setMsg('请先处理当前文件的保存冲突');
      return;
    }
    setActionBusy('discard');
    setMsg('');
    clearTimeout(saveTimerRef.current);
    try {
      await savePromiseRef.current.catch(() => {});
      const activeDraft = draftRef.current;
      if (!activeDraft) return;
      await discardPackageDraft(projectId, activeDraft.revision, activeDraft.id);
      notifyDraftChange(null);
      localChangeRef.current = false;
      handledDraftRef.current = null;
      setDiscardOpen(false);
      setView('view');
      setMsg('已放弃人工编辑草稿，恢复正式版本内容');
      await loadPkg();
      setFileReloadKey((key) => key + 1);
    } catch (e) { setMsg(`放弃失败：${e.message}`); }
    finally { setActionBusy(''); }
  };

  const download = async () => {
    setMsg('');
    try {
      const res = await requireOkResponse(await authFetch(exportPackageUrl(projectId)));
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]) || 'package.zip';
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { setMsg(`导出失败：${e.message}`); }
  };

  if (err) return <div className="empty">{err}</div>;
  if (!pkg) return <div className="empty">加载中…</div>;
  const isMd = path?.endsWith('.md');
  const draftChangedSet = new Set((packageDraft?.changedFiles ?? []).map((changed) => changed.path));
  const content = file?.content ?? '';
  const editable = isPackageDraftEditablePath(path);
  const currentDraftChanged = !!file?.changed || draftChangedSet.has(path);
  const nonPublishableDraftPaths = [...draftChangedSet].filter((changedPath) => !isPackageDraftEditablePath(changedPath));
  const migratedLegacyDraft = packageDraft?.createdBy === 'legacy-override-migration';
  // 体检发现的 target（对象/操作 key）→ 包内文件路径
  const openByTarget = (target) => {
    const hit = pkg.files.find((f) => f === target)
      ?? pkg.files.find((f) => f.endsWith(`/${target}.md`))
      ?? pkg.files.find((f) => f.includes(target));
    if (hit) selectFile(hit);
  };

  return (
    <div>
    {repairInProgress && (
      <div className="draft-chat-gate">
        <b>{PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE}</b>
        <span>{packageRepairBusy === 'apply' ? '正在应用修缮计划并生成新版本。' : '正在分析修缮影响。'}完成后可继续编辑。</span>
      </div>
    )}
    {packageDraft && (
      <div className={`draft-status-bar ${packageDraft.stale ? 'stale' : ''}`}>
        <div className="draft-status-main">
          <b>人工编辑草稿</b>
          <span>基线 v{packageDraft.baseVersion}</span>
          <span>已修改 {packageDraft.changedFileCount} 个文件</span>
          <span>revision {packageDraft.revision}</span>
          {migratedLegacyDraft && <span className="tag tag-draft">由旧版人工编辑内容迁移</span>}
          {packageDraft.stale && <span className="tag tag-err">基线已过时，请放弃后重新编辑</span>}
        </div>
        <div className="draft-status-actions">
          <span className={`draft-save-state ${saveState}`}>
            {{ idle: '草稿已同步', unsaved: '等待自动保存', saving: '自动保存中…', saved: '已自动保存', error: '保存失败' }[saveState]}
          </span>
          <button className="btn" disabled={repairInProgress || !!revisionConflict || !!actionBusy}
            title={revisionConflict ? '请先处理当前文件的保存冲突' : ''}
            onClick={() => setDiscardOpen(true)}>放弃草稿</button>
          <button className="btn btn-p" disabled={repairInProgress || !!revisionConflict || !!actionBusy || (!packageDraft.changedFileCount && !hasUnsavedContent) || packageDraft.stale || nonPublishableDraftPaths.length > 0}
            title={repairInProgress
              ? PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE
              : revisionConflict
                ? '请先处理当前文件的保存冲突'
              : packageDraft.stale
              ? '草稿基线已过时，请放弃后基于最新版重新编辑'
              : nonPublishableDraftPaths.length
                ? '请先撤销迁移草稿中的派生文件覆盖'
                : !packageDraft.changedFileCount && !hasUnsavedContent ? '草稿暂无文件变更' : ''}
            onClick={() => setPublishOpen(true)}>发布新版本</button>
        </div>
      </div>
    )}
    {nonPublishableDraftPaths.length > 0 && (
      <div className="legacy-migration-warning">
        <b>迁移草稿包含 {nonPublishableDraftPaths.length} 个派生文件覆盖</b>
        <span>这些文件现在由正式规格自动生成，不能直接发布。请在文件树中选择带“草稿”标记的派生文件，审阅后撤销对应修改，再发布其余详情文件。</span>
      </div>
    )}
    <PackageCheck projectId={projectId} onOpenFile={openByTarget} packageDraft={packageDraft} />
    <div className="rv">
      <div className="rv-files">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--ink2)' }}>v{pkg.version}</span>
          <button className="btn" style={{ marginLeft: 'auto', padding: '3px 10px', fontSize: 11.5 }} onClick={download}>导出 zip</button>
        </div>
        {pkg.files.map((f) => (
          <button key={f} className={`rv-file ${f === path ? 'on' : ''} ${isPackageDraftEditablePath(f) ? '' : 'readonly'}`}
            title={isPackageDraftEditablePath(f) ? '' : '派生文件，只读'} onClick={() => selectFile(f)}>
            <span className="rv-file-path">{f}</span>
            {draftChangedSet.has(f) && <span className="tag tag-draft rv-file-draft-tag">草稿</span>}
          </button>
        ))}
        <div className="rv-file-legend">
          <span className="tag tag-draft">草稿</span>
          <span>表示草稿已修改 · 灰色为派生只读文件</span>
        </div>
      </div>
      <div className="rv-view">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12, color: 'var(--muted)' }}>
            {path}
            {!editable && <span className="tag tag-lang" style={{ marginLeft: 8 }}>派生文件 · 只读</span>}
            {currentDraftChanged && <span className="tag tag-draft" style={{ marginLeft: 8 }}>草稿已修改</span>}
          </span>
          <span className="viewtog" style={{ marginLeft: 'auto' }}>
            <button className={view === 'view' ? 'on' : ''} onClick={showRenderedView}>查看</button>
            <button className={view === 'edit' ? 'on' : ''} disabled={repairInProgress || !editable || actionBusy === 'create'}
              title={repairInProgress ? PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE : !editable ? '派生文件不能人工编辑' : ''}
              onClick={startEditing}>{actionBusy === 'create' ? '进入编辑中…' : '编辑'}</button>
          </span>
        </div>
        {view === 'edit' ? (
          <div>
            <textarea className="rv-editor" value={editorContent} disabled={repairInProgress}
              onChange={(e) => {
                localChangeRef.current = e.target.value !== savedContentRef.current;
                setEditorContent(e.target.value);
              }} spellCheck={false} />
            {revisionConflict?.path === path && (
              <div className="draft-revision-conflict" role="alert">
                <div>
                  <b>{revisionConflict.scope === 'draft' ? '原草稿已被其他页面替换' : '检测到同文件并发修改'}</b>
                  <span>{revisionConflict.scope === 'draft'
                    ? '当前项目已有另一份活动草稿。自动保存已暂停，当前输入仍保留。'
                    : '其他页面已保存了此文件。自动保存已暂停，当前输入仍保留。'}</span>
                </div>
                <div className="draft-revision-conflict-actions">
                  <button className="btn" disabled={!!actionBusy} onClick={loadOtherPageVersion}>加载其他页面版本</button>
                  <button className="btn btn-p" disabled={!!actionBusy} onClick={overwriteRevisionConflict}>
                    {actionBusy === 'conflict-overwrite' ? '覆盖保存中…' : '保留当前内容并覆盖'}
                  </button>
                </div>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
              <button className="btn btn-p" disabled={repairInProgress || !!revisionConflict || !!actionBusy || saveState === 'saving' || editorContent === savedContentRef.current}
                onClick={saveNow}>{saveState === 'saving' ? '保存中…' : '保存到草稿'}</button>
              {(currentDraftChanged || editorContent !== file?.baseContent) && (
                <button className="btn" disabled={repairInProgress || !!revisionConflict || !!actionBusy || saveState === 'saving'} onClick={revertDraftFile}>
                  {actionBusy === 'revert' ? '撤销中…' : '撤销此文件修改'}
                </button>
              )}
              <span className={`draft-save-message ${saveState === 'error' ? 'error' : ''}`}>
                {msg || '修改会自动保存到草稿；发布后才会生成正式版本并用于消费'}
              </span>
            </div>
          </div>
        ) : isMd
          ? <div className="mdv"><ReactMarkdown remarkPlugins={[remarkGfm]}>{stripFrontmatter(content)}</ReactMarkdown></div>
          : <pre className="srcview">{content}</pre>}
        {view === 'view' && currentDraftChanged && !editable && (
          <div className="legacy-migration-file-actions">
            <button className="btn" disabled={repairInProgress || !!actionBusy} onClick={revertDraftFile}>
              {actionBusy === 'revert' ? '撤销中…' : '撤销该派生文件覆盖'}
            </button>
            <span>{msg || '派生文件会由规格重新生成；撤销后可继续发布其余详情文件修改'}</span>
          </div>
        )}
        {view === 'view' && msg && !(currentDraftChanged && !editable) && <div className="draft-view-message">{msg}</div>}
      </div>
    </div>
    <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
      <button className="btn btn-p" disabled={!!packageDraft}
        title={packageDraft ? '请先发布或放弃人工编辑草稿' : ''} onClick={onAdvance}>进入数据源配置 →</button>
    </div>
    {publishOpen && packageDraft && (
      <div className="modal-ov" role="presentation">
        <div className="modal draft-publish-modal" role="dialog" aria-modal="true" aria-labelledby="draft-publish-title">
          <h3 id="draft-publish-title">发布人工编辑草稿</h3>
          <p>将基于 v{packageDraft.baseVersion} 的 {packageDraft.changedFileCount} 个文件修改合并为一个正式新版本。发布成功后，导出、修缮和评测都会使用新版本。</p>
          <label className="field">
            <span>发布说明（可选）</span>
            <textarea rows={3} maxLength={500} value={publishSummary}
              disabled={repairInProgress}
              placeholder="例如：补充订单取消规则并修正金额口径"
              onChange={(event) => setPublishSummary(event.target.value)} />
          </label>
          <div className="foot">
            <button className="btn" disabled={actionBusy === 'publish'} onClick={() => setPublishOpen(false)}>取消</button>
            <button className="btn btn-p" disabled={repairInProgress || !!revisionConflict || actionBusy === 'publish'} onClick={doPublish}>
              {actionBusy === 'publish' ? '发布并校验中…' : '确认发布新版本'}
            </button>
          </div>
        </div>
      </div>
    )}
    {publishRevisionConflict && (
      <div className="modal-ov" role="presentation">
        <div className="modal draft-publish-conflict-modal" role="alertdialog" aria-modal="true"
          aria-labelledby="draft-publish-conflict-title" aria-describedby="draft-publish-conflict-description">
          <h3 id="draft-publish-conflict-title">当前草稿已被更新</h3>
          <p id="draft-publish-conflict-description">
            检测到有其他修改已应用到当前草稿，当前页面的 revision 已过期。请刷新页面加载最新草稿，确认修改后再重新发布新版本。
          </p>
          <div className="foot">
            <button className="btn btn-p" onClick={() => window.location.reload()}>刷新页面</button>
          </div>
        </div>
      </div>
    )}
    {discardOpen && packageDraft && (
      <div className="modal-ov" role="presentation">
        <div className="modal draft-discard-modal" role="dialog" aria-modal="true" aria-labelledby="draft-discard-title">
          <h3 id="draft-discard-title">放弃人工编辑草稿</h3>
          <p>将丢弃本次草稿中 {packageDraft.changedFileCount} 个文件的修改，并恢复正式版本 v{pkg.version}。此操作无法撤销。</p>
          <div className="foot">
            <button className="btn" disabled={actionBusy === 'discard'} onClick={() => setDiscardOpen(false)}>继续编辑</button>
            <button className="btn btn-danger" disabled={repairInProgress || !!revisionConflict || actionBusy === 'discard'} onClick={doDiscard}>
              {actionBusy === 'discard' ? '放弃中…' : '确认放弃草稿'}
            </button>
          </div>
        </div>
      </div>
    )}
    </div>
  );
}

function stripFrontmatter(md) {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(md || '');
  return m ? md.slice(m[0].length) : md;
}
