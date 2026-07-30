import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  listRounds, diagnoseOptimize, listDiagnoses, resolveDiagnosis, proposeOptimize,
  listSuggestions, applyOptimize, rejectSuggestions, listCandidateRevisions,
  previewCandidateRevision, respondCandidateRevision,
  listVersions, exportVersionUrl, authFetch, requireOkResponse,
} from './api.js';
import {
  normalizeOptimizeOperation,
  shouldPollOptimizeOperation,
} from './optimize-running-state.js';
import { latestDiagnosableRound } from './evaluation-followup.js';
import { canProposeFromDiagnoses, diagnosesForRound } from './optimization-flow.js';
import {
  candidateStatusLabel,
  linkedOptimizationCandidates,
  optimizationCandidateNotice,
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
  const [candidateBusy, setCandidateBusy] = useState('');
  const [err, setErr] = useState('');
  const [candidateResult, setCandidateResult] = useState(null);
  const [preview, setPreview] = useState(null);
  const pollRef = useRef(null);

  const load = () => Promise.all([
    listRounds(projectId).then((d) => { setRounds(d.rounds); return d.rounds; }),
    listVersions(projectId).then((d) => setVersions(d.versions)),
    listDiagnoses(projectId).then((d) => setDiagnoses(d.diagnoses)),
    listSuggestions(projectId).then((d) => {
      const runningOperation = normalizeOptimizeOperation(d.runningOperation);
      setBusy(runningOperation);
      setSuggestions(d.suggestions);
      setSel(new Set(d.suggestions.filter((s) => s.status === 'pending').map((s) => s.id)));
      return runningOperation;
    }),
    listCandidateRevisions(projectId).then((d) => setCandidates(d.candidates)),
  ]).then(([, , , runningOperation]) => {
    setErr('');
    return runningOperation;
  })
    .catch((e) => { setErr(e.message); return undefined; });
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
  const optimizationCandidates = linkedOptimizationCandidates(suggestions, candidates);

  const diagnose = async () => {
    setBusy('diagnose'); setErr(''); setCandidateResult(null);
    try {
      const d = await diagnoseOptimize(projectId, targetRound?.id);
      setDiagnoses((previous) => [
        ...previous.filter((item) => item.roundId !== targetRound?.id),
        ...d.diagnoses,
      ]);
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
      setSuggestions((prev) => {
        const known = new Set(prev.map((s) => s.id));
        return [...prev, ...d.suggestions.filter((s) => !known.has(s.id))];
      });
      setSel(new Set(d.suggestions.filter((s) => s.status === 'pending').map((s) => s.id)));
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const apply = async () => {
    setBusy('apply'); setErr('');
    try {
      const d = await applyOptimize(projectId, [...sel]);
      setCandidateResult(optimizationCandidateNotice(d));
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const reject = async (id) => {
    try {
      const d = await rejectSuggestions(projectId, [id]);
      setSuggestions(d.suggestions);
      setSel((prev) => { const n = new Set(prev); n.delete(id); return n; });
    } catch (e) { setErr(e.message); }
  };

  const toggle = (id) => setSel((prev) => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  const openCandidate = async (candidate) => {
    setCandidateBusy(candidate.id); setErr('');
    try { setPreview(await previewCandidateRevision(projectId, candidate.id)); }
    catch (e) { setErr(e.message); }
    finally { setCandidateBusy(''); }
  };

  const respondCandidate = async (candidate, action) => {
    let note = '';
    if (action === 'revise') {
      const input = window.prompt('请填写退回修改说明', '');
      if (input === null || !input.trim()) return;
      note = input.trim();
    } else if (action === 'reject') {
      const input = window.prompt('可选：填写拒绝原因', '');
      if (input === null) return;
      note = input.trim();
    }
    setCandidateBusy(candidate.id); setErr('');
    try {
      await respondCandidateRevision(projectId, candidate.id, action, undefined, note);
      setPreview(null);
      await load();
    } catch (e) { setErr(e.message); }
    finally { setCandidateBusy(''); }
  };

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
      {suggestions.length > 0 && (
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
          {pending.length > 0 && (
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

      {optimizationCandidates.length > 0 && (
        <div style={{ marginTop: 22 }}>
          <div className="sg-bar"><span className="sg-bar-t">⑤ 候选预览与决策</span></div>
          {optimizationCandidates.map((candidate) => (
            <div key={candidate.id} className="sg-card">
              <div className="sg-head">
                <span className={`tag ${candidate.status === 'accepted' ? 'tag-ok' : 'tag-lang'}`}>
                  {candidateStatusLabel(candidate)}
                </span>
                <span className="sg-title">{candidate.summary}</span>
                <span style={{ color: 'var(--muted)', fontSize: 12 }}>{candidate.id}</span>
              </div>
              <div className="sg-body">
                <button className="btn" disabled={candidateBusy === candidate.id} onClick={() => openCandidate(candidate)}>
                  {candidateBusy === candidate.id ? '加载中…' : '预览变更'}
                </button>
                {candidate.status === 'pending' && (
                  <span style={{ marginLeft: 8 }}>
                    <button className="btn btn-p" disabled={!!candidateBusy}
                      onClick={() => respondCandidate(candidate, 'accept')}>接受并形成正式修订</button>
                    <button className="btn" disabled={!!candidateBusy} style={{ marginLeft: 6 }}
                      onClick={() => respondCandidate(candidate, 'revise')}>退回修改</button>
                    <button className="btn" disabled={!!candidateBusy} style={{ marginLeft: 6 }}
                      onClick={() => respondCandidate(candidate, 'reject')}>拒绝</button>
                  </span>
                )}
                {candidate.status === 'accepted' && (
                  <button className="btn btn-p" style={{ marginLeft: 8 }} onClick={onGoEvaluate}>去评测当前修订 →</button>
                )}
                {preview?.candidate?.id === candidate.id && (
                  <div style={{ marginTop: 10 }}>
                    <b style={{ fontSize: 12 }}>变更文件</b>
                    <ul>{preview.files.map((file) => <li key={file.path}>{file.status} · {file.path}</li>)}</ul>
                    {preview.candidate.groups.flatMap((group) => group.validation.errors || []).map((message) => (
                      <div key={message} className="lerr">{message}</div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
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
