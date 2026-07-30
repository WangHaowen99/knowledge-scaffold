import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  listRounds, diagnoseOptimize, listDiagnoses, resolveDiagnosis, proposeOptimize,
  listSuggestions, getOptimizeApplyStatus, applyOptimize, reviewOptimizeApply, rejectSuggestions,
  listVersions, exportVersionUrl, authFetch, requireOkResponse,
} from './api.js';
import { listCandidateRevisions } from './authoring-api.js';
import CandidateRevisionPanel, { CandidateRevisionCanvas } from './CandidateRevisionPanel.jsx';
import {
  normalizeOptimizeOperation,
  shouldPollOptimizeOperation,
} from './optimize-running-state.js';
import { latestDiagnosableRound } from './evaluation-followup.js';
import {
  canProposeFromDiagnoses,
  diagnoseAndRefresh,
  diagnosesForRound,
  optimizationExpertReviewForRound,
  optimizationExpertReviewState,
  pendingSuggestionIdsForRound,
  suggestionsForRound,
} from './optimization-flow.js';
import {
  linkedOptimizationCandidates,
  optimizationCandidateNotice,
  pendingLinkedOptimizationCandidates,
} from './optimization-candidates.js';

/* ================= 节点 11 · 优化建议与应用 ================= */

const KIND_LABEL = {
  add_operation: '新增操作', modify_operation: '修改操作',
  add_object: '新增对象', modify_object: '修改对象',
  add_rule: '新增规则', modify_rule: '修改规则',
  add_metric: '新增指标', modify_metric: '修改指标',
  add_script: '新增脚本', modify_script: '修改脚本',
  clarify_description: '澄清描述',
};
const KIND_CLASS = {
  add_operation: 'sg-kind-add', add_object: 'sg-kind-add', add_rule: 'sg-kind-add',
  add_metric: 'sg-kind-add', add_script: 'sg-kind-add',
  modify_operation: 'sg-kind-mod', modify_object: 'sg-kind-mod', modify_rule: 'sg-kind-mod',
  modify_metric: 'sg-kind-mod', modify_script: 'sg-kind-mod',
  clarify_description: 'sg-kind-clr',
};

// 标题里形如「[add_operation] xxx」的类型前缀与左侧类型标签重复，展示时剥掉
const cleanTitle = (title = '') => title.replace(/^\s*[\[［][a-z_ ]+[\]］]\s*/i, '');

function SuggestionCard({ s, checked, onToggle, onReject }) {
  const pending = s.status === 'pending';
  const [open, setOpen] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const bodyRef = useRef(null);

  // 折叠态下检测正文是否溢出，只有溢出才显示「展开详情」；宽度变化（窗口缩放）后需重测
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const measure = () => { if (!open) setOverflowing(el.scrollHeight > el.clientHeight + 1); };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [s.detail, s.relatedQuestion, open]);

  return (
    <div className={`sg-card ${pending ? '' : 'settled'}`}>
      <div className="sg-head" style={{ cursor: overflowing || open ? 'pointer' : 'default' }}
        onClick={() => (overflowing || open) && setOpen((v) => !v)}>
        {pending
          ? <input type="checkbox" aria-label={cleanTitle(s.title) || s.title} checked={checked}
              onClick={(e) => e.stopPropagation()} onChange={onToggle} />
          : <span className={`tag ${s.status === 'applied' ? 'tag-ok' : 'tag-lang'}`}>
              {s.status === 'candidate'
                ? '候选待确认'
                : s.status === 'applied'
                  ? `已接受 ${String(s.appliedRevision || '').slice(0, 8)}`
                  : '已拒绝'}
            </span>}
        <span className={`tag sg-kind ${KIND_CLASS[s.kind] || 'sg-kind-mod'}`}>{KIND_LABEL[s.kind] ?? s.kind}</span>
        <span className="sg-title" title={s.title}>{cleanTitle(s.title) || s.title}</span>
        {pending && (
          <button className="tc-del" title="忽略该建议"
            onClick={(e) => { e.stopPropagation(); onReject(); }}>✕</button>
        )}
      </div>
      <div ref={bodyRef} className={`sg-body ${open ? '' : 'clamp'}${!open && overflowing ? ' fade' : ''}`}>
        <div className="mdv sg-md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{s.detail || ''}</ReactMarkdown></div>
        {s.relatedQuestion && (
          <div className="sg-q"><b>关联用例</b><div>{s.relatedQuestion}</div></div>
        )}
      </div>
      {(overflowing || open) && (
        <button className="sg-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? '收起 ▲' : '展开详情 ▼'}
        </button>
      )}
    </div>
  );
}

export function OptimizePanel({ projectId, onGoEvaluate }) {
  const [rounds, setRounds] = useState(null);
  const [versions, setVersions] = useState([]);
  const [diagnoses, setDiagnoses] = useState([]);
  const [suggestions, setSuggestions] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [sel, setSel] = useState(new Set());
  const [busy, setBusy] = useState('');   // '' | diagnose | propose | apply
  const [err, setErr] = useState('');
  const [candidateResult, setCandidateResult] = useState(null);
  const [applyReview, setApplyReview] = useState(null);
  const [preview, setPreview] = useState(null);
  const pollRef = useRef(null);

  const load = async () => {
    try {
      const roundData = await listRounds(projectId);
      const nextTargetRound = latestDiagnosableRound(roundData.rounds);
      const roundId = nextTargetRound?.id;
      const [versionData, diagnosisData, suggestionData, candidateData, applyStatus] = await Promise.all([
        listVersions(projectId),
        listDiagnoses(projectId, roundId),
        listSuggestions(projectId, roundId),
        listCandidateRevisions(projectId),
        getOptimizeApplyStatus(projectId),
      ]);
      const roundSuggestions = suggestionsForRound(suggestionData.suggestions, roundId);
      const runningOperation = normalizeOptimizeOperation(suggestionData.runningOperation);
      setRounds(roundData.rounds);
      setVersions(versionData.versions);
      setDiagnoses(diagnosesForRound(diagnosisData.diagnoses, roundId));
      setSuggestions(roundSuggestions);
      setSel(new Set(pendingSuggestionIdsForRound(roundSuggestions, roundId)));
      setCandidates(candidateData.candidates);
      setApplyReview(optimizationExpertReviewForRound(applyStatus, roundSuggestions, roundId));
      setBusy(runningOperation);
      setErr('');
      return runningOperation;
    } catch (e) {
      setErr(e.message);
      return undefined;
    }
  };
  useEffect(() => {
    let cancelled = false;
    const refreshUntilIdle = async () => {
      const runningOperation = await load();
      if (!cancelled && shouldPollOptimizeOperation(runningOperation)) {
        pollRef.current = setTimeout(refreshUntilIdle, 2000);
      }
    };
    refreshUntilIdle();
    return () => {
      cancelled = true;
      clearTimeout(pollRef.current);
    };
  }, [projectId]);

  if (!rounds) return <div className="empty">{err || '加载中…'}</div>;
  const targetRound = latestDiagnosableRound(rounds);
  const roundDiagnoses = diagnosesForRound(diagnoses, targetRound?.id);
  const canPropose = canProposeFromDiagnoses(roundDiagnoses);
  const pending = suggestions.filter((s) => s.status === 'pending');
  const linkedCandidates = linkedOptimizationCandidates(suggestions, candidates);
  const optimizationCandidates = pendingLinkedOptimizationCandidates(suggestions, candidates);
  const hasAcceptedOptimization = linkedCandidates.some((candidate) => candidate.status === 'accepted');

  const diagnose = async () => {
    setBusy('diagnose'); setErr(''); setCandidateResult(null);
    try {
      await diagnoseAndRefresh(
        () => diagnoseOptimize(projectId, targetRound?.id),
        async () => { setPreview(null); await load(); },
      );
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const resolve = async (diagnosis, action) => {
    const needsNote = ['confirm_rule', 'clarify_semantics', 'not_applicable'].includes(action);
    const note = window.prompt(needsNote ? '请填写专家确认依据' : '可选：填写处置说明', '');
    if (note === null || (needsNote && !note.trim())) return;
    try {
      const d = await resolveDiagnosis(projectId, diagnosis.id, action, note);
      setDiagnoses((previous) => [
        ...previous.filter((item) => item.roundId !== diagnosis.roundId),
        ...d.diagnoses,
      ]);
    } catch (e) { setErr(e.message); }
  };

  const propose = async () => {
    setBusy('propose'); setErr(''); setCandidateResult(null);
    try {
      const d = await proposeOptimize(projectId, targetRound?.id);
      const roundSuggestions = suggestionsForRound(d.suggestions, targetRound?.id);
      setSuggestions(roundSuggestions);
      setSel(new Set(pendingSuggestionIdsForRound(roundSuggestions, targetRound?.id)));
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const apply = async () => {
    setBusy('apply'); setErr('');
    try {
      const selectedSuggestionIds = pendingSuggestionIdsForRound(suggestions, targetRound?.id)
        .filter((id) => sel.has(id));
      if (!selectedSuggestionIds.length) throw new Error('请选择当前评测轮次的待处理建议');
      const d = await applyOptimize(projectId, selectedSuggestionIds);
      const review = optimizationExpertReviewState(d);
      if (review) {
        setApplyReview(review);
        setCandidateResult(null);
      } else {
        setApplyReview(null);
        setCandidateResult(optimizationCandidateNotice(d));
      }
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const reviewApply = async (action) => {
    if (!applyReview) return;
    let note = '';
    if (action === 'retry') {
      const input = window.prompt('可选：补充本次重试必须满足的约束', '');
      if (input === null) return;
      note = input.trim();
    } else if (!window.confirm('确认跳过本次所选优化建议？')) return;
    setBusy('apply'); setErr(''); setCandidateResult(null);
    try {
      const reviewed = await reviewOptimizeApply(
        projectId, applyReview.taskId, action, note,
      );
      setApplyReview(null);
      if (action === 'retry') {
        const d = await applyOptimize(projectId, reviewed.suggestionIds);
        const nextReview = optimizationExpertReviewState(d);
        if (nextReview) setApplyReview(nextReview);
        else setCandidateResult(optimizationCandidateNotice(d));
      }
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const reject = async (id) => {
    try {
      await rejectSuggestions(projectId, [id]);
      await load();
    } catch (e) { setErr(e.message); }
  };

  const toggle = (id) => setSel((prev) => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  return (
    <div>
      {err && <div className="lerr">{err}</div>}

      {/* 失败诊断与专家处置 */}
      <div className="ev-launch">
        {targetRound
          ? <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
              基于轮次：包 v{targetRound.packageVersion} · 修订 {String(targetRound.artifactRevision || '').slice(0, 8)} ·
              通过 {targetRound.passCount}/{targetRound.progressTotal}
            </div>
          : <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>暂无需要诊断的终态评测轮次</div>}
        <button className="btn" disabled={!!busy || !targetRound} onClick={diagnose}>
          {busy === 'diagnose' ? '正在分析失败原因…' : '① 诊断失败原因'}
        </button>
        <button className="btn btn-p" style={{ marginLeft: 8 }}
          disabled={!!busy || !targetRound || !roundDiagnoses.length || !canPropose}
          onClick={propose}>
          {busy === 'propose' ? '正在生成建议…' : '③ 生成优化建议'}
        </button>
        {busy && (
          <span style={{ marginLeft: 10, fontSize: 12.5, color: 'var(--muted)' }}>
            优化任务正在执行，请稍候
          </span>
        )}
      </div>

      {roundDiagnoses.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="sg-bar"><span className="sg-bar-t">② 专家处置失败诊断</span></div>
          {roundDiagnoses.map((diagnosis) => (
            <div key={diagnosis.id} className="sg-card">
              <div className="sg-head">
                <span className={`tag ${diagnosis.packageOptimizable && !diagnosis.blocking ? 'tag-ok' : 'tag-draft'}`}>
                  {diagnosis.packageOptimizable && !diagnosis.blocking ? '可优化' : '需处置'}
                </span>
                <span className="sg-title">{diagnosis.title}</span>
              </div>
              <div className="sg-body">
                <div>{diagnosis.summary}</div>
                {diagnosis.evidence?.length > 0 && <div className="sg-q"><b>证据</b><div>{diagnosis.evidence.join('；')}</div></div>}
                {diagnosis.resolutionStatus !== 'pending'
                  ? <div style={{ marginTop: 8, color: 'var(--muted)', fontSize: 12 }}>
                      已处置：{diagnosis.resolutionStatus}{diagnosis.resolutionNote ? ` · ${diagnosis.resolutionNote}` : ''}
                    </div>
                  : <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 9 }}>
                      <button className="btn" onClick={() => resolve(diagnosis, 'confirm_rule')}>确认规则</button>
                      <button className="btn" onClick={() => resolve(diagnosis, 'clarify_semantics')}>澄清语义</button>
                      <button className="btn" onClick={() => resolve(diagnosis, 'data_updated')}>数据已修复</button>
                      <button className="btn" onClick={() => resolve(diagnosis, 'not_applicable')}>不适用</button>
                      <button className="btn" onClick={() => resolve(diagnosis, 'defer')}>暂缓</button>
                    </div>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 建议列表 */}
      {(suggestions.length > 0 || applyReview) && (
        <div style={{ marginTop: 16 }}>
          <div className="sg-bar">
            <span className="sg-bar-t">优化建议</span>
            <span className="sg-bar-c">{pending.length > 0 ? `${sel.size}/${pending.length} 条待处理已选` : '均已处理'}</span>
            {pending.length > 0 && (
              <span className="sg-bar-ops">
                <button onClick={() => setSel(new Set(pending.map((s) => s.id)))}>全选</button>
                <i>·</i>
                <button onClick={() => setSel(new Set())}>清空</button>
              </span>
            )}
          </div>
          {suggestions.map((s) => (
            <SuggestionCard key={s.id} s={s} checked={sel.has(s.id)}
              onToggle={() => toggle(s.id)} onReject={() => reject(s.id)} />
          ))}
          {applyReview && (
            <div className="sg-card" style={{ marginTop: 10 }}>
              <div className="sg-head">
                <span className="tag tag-draft">等待专家决策</span>
                <span className="sg-title">自动修订两次未通过服务端校验</span>
              </div>
              <div className="sg-body">
                {applyReview.error && <div>{applyReview.error}</div>}
                {applyReview.validationErrors.length > 0 && (
                  <ul>{applyReview.validationErrors.map((message) => <li key={message}>{message}</li>)}</ul>
                )}
                <button className="btn btn-p" disabled={!!busy}
                  onClick={() => reviewApply('retry')}>带反馈重试</button>
                <button className="btn" style={{ marginLeft: 8 }} disabled={!!busy}
                  onClick={() => reviewApply('skip')}>跳过所选建议</button>
              </div>
            </div>
          )}
          {pending.length > 0 && !applyReview && (
            <button className="btn btn-p" style={{ marginTop: 8 }} disabled={!!busy || !sel.size} onClick={apply}>
              {busy === 'apply' ? '正在生成待确认候选…' : `④ 生成包含 ${sel.size} 条建议的待确认候选`}
            </button>
          )}
        </div>
      )}

      {candidateResult && (
        <div className="sg-applied">
          ✓ 已生成待确认候选 <b>{candidateResult.candidateId}</b>（包含 {candidateResult.selected} 条建议）。
          接受前不会推进正式修订。
        </div>
      )}

      {(optimizationCandidates.length > 0 || hasAcceptedOptimization) && (
        <div style={{ marginTop: 22 }}>
          {optimizationCandidates.length > 0 && (
            <CandidateRevisionPanel projectId={projectId} candidates={optimizationCandidates}
              title="⑤ 优化候选预览与决策"
              subtitle="接受前不推进正式修订；退回、拒绝或失效会同步优化建议状态"
              onPreview={setPreview} onChanged={async () => {
                setPreview(null);
                setCandidateResult(null);
                await load();
              }} onError={setErr} />
          )}
          <CandidateRevisionCanvas preview={preview} onClose={() => setPreview(null)} />
          {hasAcceptedOptimization && (
            <button className="btn btn-p" style={{ marginTop: 8 }} onClick={onGoEvaluate}>去评测当前修订 →</button>
          )}
        </div>
      )}

      {/* 版本历史与交付 */}
      {versions.length > 0 && (
        <div style={{ marginTop: 22 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>版本历史与交付</div>
          <VersionTable projectId={projectId} versions={versions} />
        </div>
      )}
    </div>
  );
}

export function VersionTable({ projectId, versions }) {
  const [dlErr, setDlErr] = useState('');
  const download = async (version) => {
    setDlErr('');
    try {
      const res = await requireOkResponse(await authFetch(exportVersionUrl(projectId, version)));
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]) || `package-${version}.zip`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { setDlErr(`导出失败：${e.message}`); }
  };

  return (
    <>
    {dlErr && <div style={{ color: 'var(--crit)', fontSize: 12, marginBottom: 6 }}>{dlErr}</div>}
    <table className="dt">
      <thead><tr><th>版本</th><th>说明</th><th>评测通过率</th><th></th></tr></thead>
      <tbody>
        {versions.map((v, i) => {
          // v.rounds 按创建时间倒序，[0] 即该版本最新一轮
          const done = v.rounds.filter((r) => r.status === 'done');
          const best = done.length ? done[0] : null;
          const prev = versions[i - 1];
          const prevDone = prev ? prev.rounds.filter((r) => r.status === 'done') : [];
          const prevBest = prevDone.length ? prevDone[0] : null;
          const improved = best && prevBest && best.progressTotal === prevBest.progressTotal && best.passCount > prevBest.passCount;
          return (
            <tr key={v.id}>
              <td style={{ fontFamily: 'ui-monospace, monospace', fontWeight: 650 }}>v{v.version}</td>
              <td style={{ color: 'var(--ink2)' }}>{v.note || '初始生成'}</td>
              <td>
                {best
                  ? <span>{best.passCount}/{best.progressTotal}{improved ? <span className="sg-up"> ↑提升</span> : ''}</span>
                  : <span style={{ color: 'var(--muted)' }}>未评测</span>}
              </td>
              <td><button className="btn" style={{ padding: '3px 10px', fontSize: 11.5 }} onClick={() => download(v.version)}>导出 zip</button></td>
            </tr>
          );
        })}
      </tbody>
    </table>
    </>
  );
}
