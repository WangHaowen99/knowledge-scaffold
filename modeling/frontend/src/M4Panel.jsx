import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  listRounds, diagnoseOptimize, listDiagnoses, resolveDiagnosis, proposeOptimize, listSuggestions, applyOptimize, rejectSuggestions, getOptimizeTask,
  getOptimizeApplyStatus, reviewOptimizeApply, listVersions, exportVersionUrl, authFetch,
  planEvaluationDefectRepair,
} from './api.js';
import {
  canPlanEvaluationRepair,
  selectableEvaluationDefects,
  submittableEvaluationDefectIds,
  toggleEvaluationDefect,
} from './evaluationDefects.js';
import OptimizationApplyProgress from './OptimizationApplyProgress.jsx';
import OptimizationOperationProgress from './OptimizationOperationProgress.jsx';
import { latestDiagnosableRound } from './evaluation-followup.js';
import {
  normalizeOptimizeOperation,
  shouldPollOptimizeOperation,
} from './optimize-running-state.js';
import { createLatestRequestGate } from './packageFileContent.js';

/* ================= 节点 14/15 · 失败诊断 / 优化更新 ================= */

const KIND_LABEL = {
  add_operation: '新增操作', modify_operation: '修改操作',
  add_object: '新增对象', modify_object: '修改对象',
  add_rule: '新增规则', modify_rule: '修改规则',
  add_metric: '新增指标', modify_metric: '修改指标',
  add_script: '新增脚本', modify_script: '修改脚本',
  clarify_description: '澄清描述',
};
const KIND_CLASS = {
  add_operation: 'sg-kind-add', add_object: 'sg-kind-add', add_rule: 'sg-kind-add', add_metric: 'sg-kind-add', add_script: 'sg-kind-add',
  modify_operation: 'sg-kind-mod', modify_object: 'sg-kind-mod', modify_rule: 'sg-kind-mod', modify_metric: 'sg-kind-mod', modify_script: 'sg-kind-mod',
  clarify_description: 'sg-kind-clr',
};

// 标题里形如「[add_operation] xxx」的类型前缀与左侧类型标签重复，展示时剥掉
const cleanTitle = (title = '') => title.replace(/^\s*[\[［][a-z_ ]+[\]］]\s*/i, '');

export function optimizationApplyFailureMessage(task) {
  if (task?.node?.status !== 'failed') return '';
  return String(task.node.reason || task.node.output?.error || '优化候选生成失败，请查看任务记录后重试或忽略建议。').trim();
}

export function optimizationApplyTaskForSuggestions(task, suggestions = []) {
  if (!task?.node) return null;
  const currentIds = new Set(suggestions.map((suggestion) => suggestion.id));
  const taskIds = task.node.output?.suggestionIds;
  if (!Array.isArray(taskIds) || !taskIds.length) {
    return currentIds.size ? null : task;
  }
  return taskIds.some((id) => currentIds.has(id)) ? task : null;
}

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
                : s.status === 'applied' ? `已应用 ${s.appliedVersion}` : '已忽略'}
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
        {!!s.evidence?.length && (
          <div className="sg-q"><b>建议依据</b><div>{s.evidence.map((item, index) => <div key={index}>• {item}</div>)}</div></div>
        )}
        {s.generalizationPlan && <div className="sg-q"><b>通用化方案</b><div>{s.generalizationPlan}</div></div>}
        {s.validationPlan && <div className="sg-q"><b>验证方案</b><div>{s.validationPlan}</div></div>}
      </div>
      {(overflowing || open) && (
        <button className="sg-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? '收起 ▲' : '展开详情 ▼'}
        </button>
      )}
    </div>
  );
}

const SCOPE_LABEL = {
  testcase: '评测用例', runtime: '运行环境', data: '数据资料', package: '知识包', script: '计算脚本', judge: '判分依据',
};

const RESOLUTION_LABEL = {
  pending: '待处理', confirmed: '已确认可修包', resolved: '外部问题已处理',
  deferred: '已暂缓', irrelevant: '已标记不适用',
};

function DiagnosisCard({
  diagnosis,
  currentRevisionOid = '',
  resolving,
  onResolve,
  selected = false,
  onToggleSelect,
}) {
  const tone = diagnosis.blocking || !diagnosis.packageOptimizable ? 'tag-lang' : 'tag-ok';
  const selectable = canPlanEvaluationRepair(diagnosis, currentRevisionOid);
  const askAndResolve = (action, prompt, optional = false) => {
    const note = window.prompt(prompt, diagnosis.resolutionNote || '');
    if (note === null || (!optional && !note.trim())) return;
    onResolve(action, note.trim());
  };
  return (
    <div className="sg-card" style={{ marginBottom: 8 }}>
      <div className="sg-head">
        {selectable && (
          <label className="dg-pick" title="选中后可一起转为建模修缮任务">
            <input type="checkbox" checked={selected}
              aria-label={`选择「${diagnosis.title}」转为建模修缮`}
              onChange={() => onToggleSelect?.(diagnosis.id)} />
          </label>
        )}
        <span className={`tag ${tone}`}>{SCOPE_LABEL[diagnosis.repairScope] ?? diagnosis.repairScope}</span>
        <span className="sg-title">{diagnosis.title}</span>
        <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
          {diagnosis.confidence === 'high' ? '高置信度' : diagnosis.confidence === 'medium' ? '中置信度' : '低置信度'}
        </span>
      </div>
      <div className="sg-body">
        <div>{diagnosis.summary}</div>
        {!!diagnosis.evidence?.length && (
          <div className="sg-q"><b>证据</b><div>{diagnosis.evidence.map((item, index) => <div key={index}>• {item}</div>)}</div></div>
        )}
        <div className="sg-q"><b>建议先处理</b><div>{diagnosis.suggestedAction}</div></div>
        {diagnosis.resolutionStatus && diagnosis.resolutionStatus !== 'pending' && (
          <div className="sg-q"><b>{diagnosis.resolutionAction === 'plan_repair' ? '建模修缮' : '专家处置'}</b><div>
            {diagnosis.resolutionAction === 'plan_repair'
              ? '已加入建模修缮'
              : (RESOLUTION_LABEL[diagnosis.resolutionStatus] ?? diagnosis.resolutionStatus)}
            {diagnosis.resolutionNote ? `：${diagnosis.resolutionNote}` : ''}
          </div></div>
        )}
        {!diagnosis.packageOptimizable && (
          <div style={{ marginTop: 7, color: 'var(--warn, #9a6700)', fontSize: 12 }}>该问题不应通过修改知识包解决。</div>
        )}
        {(diagnosis.blocking || !diagnosis.packageOptimizable) && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 9 }}>
            <button className="btn" disabled={resolving} onClick={() => askAndResolve(
              'confirm_rule', '请填写要补充或确认的业务规则：',
            )}>补充/确认规则</button>
            <button className="btn" disabled={resolving} onClick={() => askAndResolve(
              'clarify_semantics', '请填写字段、指标或对象的权威语义：',
            )}>澄清语义</button>
            <button className="btn" disabled={resolving} onClick={() => askAndResolve(
              'data_updated', '可选：说明已更新的数据或连接：', true,
            )}>数据已更新</button>
            <button className="btn" disabled={resolving} onClick={() => askAndResolve(
              'not_applicable', '请说明该诊断不适用于当前业务的原因：',
            )}>标记不适用</button>
            <button className="btn" disabled={resolving} onClick={() => askAndResolve(
              'defer', '可选：记录暂缓原因和后续责任人：', true,
            )}>暂缓处理</button>
          </div>
        )}
      </div>
    </div>
  );
}

export function FailureDiagnosisPanel({
  projectId,
  currentRevisionOid = '',
  onAdvance,
  onGoEvaluate,
  onChanged,
}) {
  const [rounds, setRounds] = useState(null);
  const [diagnoses, setDiagnoses] = useState([]);
  const [diagnosisCanPropose, setDiagnosisCanPropose] = useState(false);
  const [diagnosisNotice, setDiagnosisNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [operationTask, setOperationTask] = useState(null);
  const [resolvingId, setResolvingId] = useState('');
  const [selectedDefects, setSelectedDefects] = useState(() => new Set());
  const [repairNotice, setRepairNotice] = useState('');
  const [err, setErr] = useState('');
  const pollRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const roundData = await listRounds(projectId);
      setRounds(roundData.rounds);
      const target = latestDiagnosableRound(roundData.rounds);
      if (target) {
        const [diagnosisData, suggestionData] = await Promise.all([
          listDiagnoses(projectId, target.id),
          listSuggestions(projectId, target.id),
        ]);
        setDiagnoses(diagnosisData.diagnoses);
        setDiagnosisCanPropose(!!diagnosisData.canProposeOptimizations);
        const runningOperation = normalizeOptimizeOperation(suggestionData.runningOperation);
        const diagnosing = runningOperation === 'diagnose';
        setBusy(diagnosing ? runningOperation : '');
        setOperationTask((current) => {
          if (!diagnosing) return null;
          if (suggestionData.runningTask) return suggestionData.runningTask;
          if (current?.operation === 'diagnose') return current;
          return { operation: 'diagnose', startedAt: new Date().toISOString() };
        });
        setErr('');
        return runningOperation;
      } else {
        setDiagnoses([]);
        setDiagnosisCanPropose(false);
      }
      setErr('');
      return '';
    } catch (e) {
      setErr(e.message);
      return undefined;
    }
  }, [projectId]);
  useEffect(() => {
    let mounted = true;
    const refreshUntilIdle = async () => {
      const runningOperation = await load();
      if (mounted && runningOperation === 'diagnose') {
        pollRef.current = setTimeout(refreshUntilIdle, 2000);
      }
    };
    refreshUntilIdle();

    const task = getOptimizeTask(projectId);
    if (task?.kind === 'diagnose') {
      setBusy(task.kind);
      setOperationTask({ operation: task.kind, startedAt: task.startedAt });
      task.promise
        .then(async () => {
          if (!mounted) return;
          await load();
          await onChanged?.();
        })
        .catch((e) => mounted && setErr(e.message))
        .finally(() => {
          if (!mounted) return;
          setBusy('');
          setOperationTask(null);
        });
    }

    return () => {
      mounted = false;
      clearTimeout(pollRef.current);
    };
  }, [projectId, load, onChanged]);

  if (!rounds) return <div className="empty">{err || '加载中…'}</div>;
  const targetRound = latestDiagnosableRound(rounds);
  const currentDiagnoses = diagnoses.filter((diagnosis) => diagnosis.roundId === targetRound?.id);
  const canPropose = diagnosisCanPropose;
  const latestAnalysisAt = currentDiagnoses.map((diagnosis) => diagnosis.createdAt).filter(Boolean).sort().at(-1);
  const diagnosing = busy === 'diagnose';

  const diagnose = async () => {
    setBusy('diagnose'); setErr(''); setDiagnosisNotice('');
    setOperationTask({ operation: 'diagnose', startedAt: new Date().toISOString() });
    try {
      const data = await diagnoseOptimize(projectId, targetRound?.id);
      setDiagnoses(data.diagnoses);
      setDiagnosisCanPropose(!!data.canProposeOptimizations);
      const analyzedAt = data.diagnoses.map((diagnosis) => diagnosis.createdAt).filter(Boolean).sort().at(-1);
      setDiagnosisNotice(`已重新分析，发现 ${data.diagnoses.length} 项原因${analyzedAt ? `（${new Date(analyzedAt).toLocaleString()}）` : ''}`);
      await load();
      await onChanged?.();
    } catch (e) { setErr(e.message); } finally { setBusy(''); setOperationTask(null); }
  };

  const packageDefects = selectableEvaluationDefects(currentDiagnoses, currentRevisionOid);
  const plannableDefectIds = submittableEvaluationDefectIds(
    currentDiagnoses,
    selectedDefects,
    currentRevisionOid,
  );
  const packageDiagnosisCount = currentDiagnoses.filter((diagnosis) => diagnosis.repairScope === 'package').length;
  const stalePackageDefectCount = currentRevisionOid
    ? currentDiagnoses.filter((diagnosis) => (
      diagnosis.repairScope === 'package'
        && diagnosis.basedRevisionOid !== currentRevisionOid
    )).length
    : 0;
  const revisionMissingForRepair = !currentRevisionOid && packageDiagnosisCount > 0;

  const planRepair = async () => {
    if (!targetRound || !plannableDefectIds.length) return;
    setBusy('plan_repair'); setErr(''); setRepairNotice('');
    try {
      const data = await planEvaluationDefectRepair(projectId, targetRound.id, plannableDefectIds);
      if (Array.isArray(data.diagnoses)) setDiagnoses(data.diagnoses);
      setDiagnosisCanPropose(!!data.canProposeOptimizations);
      setSelectedDefects(new Set());
      setRepairNotice(`已把 ${plannableDefectIds.length} 项缺陷交回建模，请在右侧建模助手里继续。`);
      // 助手栏与工作流步骤都读 session，需要一次刷新才能看到新任务
      await onChanged?.();
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const resolve = async (diagnosis, action, note) => {
    setResolvingId(diagnosis.id); setErr(''); setDiagnosisNotice('');
    try {
      const data = await resolveDiagnosis(projectId, diagnosis.id, action, note);
      setDiagnoses(data.diagnoses);
      setDiagnosisCanPropose(!!data.canProposeOptimizations);
      setDiagnosisNotice(`已记录专家处置；建议后续步骤：${(data.nextSteps ?? []).join('、')}`);
      await onChanged?.();
    } catch (e) { setErr(e.message); } finally { setResolvingId(''); }
  };

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      <div className="ev-launch">
        {targetRound
          ? <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
              基于轮次：包 v{targetRound.packageVersion} · {targetRound.status === 'failed'
                ? '评测运行失败'
                : `通过 ${targetRound.passCount}/${targetRound.progressTotal}`}
            </div>
          : <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>最近没有需要诊断的评测轮次</div>}
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>分析失败原因</div>
        <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
          区分运行环境、数据、用例和知识包问题，再决定是否进入优化更新。
        </div>
        <button className="btn btn-p" disabled={!!busy || !targetRound} onClick={diagnose}>
          {busy === 'diagnose' ? '分析中…' : currentDiagnoses.length ? '重新分析' : '开始诊断'}
        </button>
        {diagnosisNotice && <div style={{ marginTop: 9, fontSize: 12.5, color: 'var(--ok, #1a7f37)' }}>{diagnosisNotice}</div>}
        {!diagnosisNotice && latestAnalysisAt && (
          <div style={{ marginTop: 9, fontSize: 12.5, color: 'var(--muted)' }}>
            最近分析时间：{new Date(latestAnalysisAt).toLocaleString()}
          </div>
        )}
      </div>

      {diagnosing && operationTask && (
        <div style={{ marginTop: 12 }}><OptimizationOperationProgress task={operationTask} /></div>
      )}

      {!diagnosing && currentDiagnoses.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="sg-bar">
            <span className="sg-bar-t">失败原因</span>
            <span className="sg-bar-c">{currentDiagnoses.length} 项，{packageDefects.length} 项可回流修缮</span>
          </div>
          {revisionMissingForRepair && (
            <div style={{ marginTop: 8, fontSize: 12.5, color: 'var(--warn, #9a6700)' }}>
              当前知识包不存在，请重新生成并评测当前修订后再回流建模。
            </div>
          )}
          {!revisionMissingForRepair && stalePackageDefectCount > 0 && (
            <div style={{ marginTop: 8, fontSize: 12.5, color: 'var(--warn, #9a6700)' }}>
              有 {stalePackageDefectCount} 项知识包诊断基于旧工件修订，不能直接回流当前知识包；请对当前修订重新评测并诊断。
            </div>
          )}
          {currentDiagnoses.map((diagnosis) => (
            <DiagnosisCard key={diagnosis.id} diagnosis={diagnosis}
              currentRevisionOid={currentRevisionOid}
              resolving={resolvingId === diagnosis.id}
              selected={selectedDefects.has(diagnosis.id)}
              onToggleSelect={(id) => setSelectedDefects((current) => toggleEvaluationDefect(current, id))}
              onResolve={(action, note) => resolve(diagnosis, action, note)} />
          ))}
        </div>
      )}

      {!diagnosing && packageDefects.length > 0 && (
        <div className="dg-to-modeling">
          <div>
            <strong>把知识包缺陷交回建模</strong>
            <p>
              选中的缺陷会登记成一条建模修缮任务，并在右侧建模助手里列出下一步。
              知识包不会被直接修改；建模已完成的项目会重新打开修缮与校验阶段。
            </p>
          </div>
          <div className="dg-to-modeling-actions">
            <button type="button" className="btn"
              disabled={busy === 'plan_repair'}
              onClick={() => setSelectedDefects(new Set(packageDefects.map((item) => item.id)))}>
              全选 {packageDefects.length} 项
            </button>
            <button type="button" className="btn btn-p"
              disabled={!plannableDefectIds.length || busy === 'plan_repair'}
              onClick={planRepair}>
              {busy === 'plan_repair'
                ? '正在交回建模…'
                : `转为建模修缮任务${plannableDefectIds.length ? `（${plannableDefectIds.length}）` : ''}`}
            </button>
          </div>
          {repairNotice && <div className="dg-to-modeling-ok">{repairNotice}</div>}
        </div>
      )}

      {!diagnosing && currentDiagnoses.length > 0 && (
        <div className="sg-applied">
          {canPropose
            ? <>诊断已完成，可以进入优化更新。<button className="btn btn-p" style={{ marginLeft: 12 }} onClick={onAdvance}>进入优化更新</button></>
            : <>当前问题需要先修复运行环境、数据或用例，再重新评测。<button className="btn" style={{ marginLeft: 12 }} onClick={onGoEvaluate}>返回评测</button></>}
        </div>
      )}
    </div>
  );
}

export function OptimizationPanel({
  projectId,
  onGoDiagnose,
  onChanged,
  onCandidateCreated,
  active = true,
}) {
  const [rounds, setRounds] = useState(null);
  const [versions, setVersions] = useState([]);
  const [diagnoses, setDiagnoses] = useState([]);
  const [diagnosisCanPropose, setDiagnosisCanPropose] = useState(false);
  const [suggestions, setSuggestions] = useState([]);
  const [sel, setSel] = useState(new Set());
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [candidateResult, setCandidateResult] = useState(null);
  const [applyTask, setApplyTask] = useState(null);
  const [expertNote, setExpertNote] = useState('');
  const [operationTask, setOperationTask] = useState(null);
  const pollRef = useRef(null);
  const announcedCandidateIdRef = useRef('');
  const loadRequestGate = useRef(null);
  if (!loadRequestGate.current) loadRequestGate.current = createLatestRequestGate();
  // 记录已见过的待处理建议：刷新时保留用户勾选，只把新出现的建议默认选上
  const knownPendingRef = useRef(new Set());

  const showCandidate = useCallback((result) => {
    if (!result?.candidateId) return;
    setCandidateResult(result);
    if (announcedCandidateIdRef.current === result.candidateId) return;
    announcedCandidateIdRef.current = result.candidateId;
    onCandidateCreated?.(result);
  }, [onCandidateCreated]);

  const load = useCallback(async () => {
    const isCurrent = loadRequestGate.current.begin();
    try {
      const roundData = await listRounds(projectId);
      if (!isCurrent()) return null;
      const target = latestDiagnosableRound(roundData.rounds);
      const [versionData, suggestionData, applyTaskData, diagnosisData] = await Promise.all([
        listVersions(projectId), listSuggestions(projectId, target?.id), getOptimizeApplyStatus(projectId),
        target ? listDiagnoses(projectId, target.id) : Promise.resolve({ diagnoses: [], canProposeOptimizations: false }),
      ]);
      if (!isCurrent()) return null;
      setRounds(roundData.rounds);
      setVersions(versionData.versions);
      setSuggestions(suggestionData.suggestions);
      setApplyTask(applyTaskData.task);
      setDiagnoses(diagnosisData.diagnoses);
      setDiagnosisCanPropose(!!diagnosisData.canProposeOptimizations);
      const runningOperation = normalizeOptimizeOperation(suggestionData.runningOperation);
      setBusy(runningOperation);
      setOperationTask((current) => {
        if (!runningOperation) return null;
        if (suggestionData.runningTask) return suggestionData.runningTask;
        if (current?.operation === runningOperation) return current;
        return { operation: runningOperation, startedAt: new Date().toISOString() };
      });
      setSel((prev) => {
        const pendingIds = suggestionData.suggestions.filter((s) => s.status === 'pending').map((s) => s.id);
        const known = knownPendingRef.current;
        const next = new Set(pendingIds.filter((id) => prev.has(id) || !known.has(id)));
        knownPendingRef.current = new Set(pendingIds);
        return next;
      });
      setErr('');
      return runningOperation;
    } catch (e) {
      if (!isCurrent()) return null;
      setErr(e.message);
      return undefined;
    }
  }, [projectId]);

  useEffect(() => {
    if (!active) {
      loadRequestGate.current.invalidate();
      clearTimeout(pollRef.current);
      return undefined;
    }
    let mounted = true;
    const refreshUntilIdle = async () => {
      const runningOperation = await load();
      if (mounted && shouldPollOptimizeOperation(runningOperation)) {
        pollRef.current = setTimeout(refreshUntilIdle, 2000);
      }
    };
    refreshUntilIdle();
    const task = getOptimizeTask(projectId);
    if (task && ['propose', 'apply'].includes(task.kind)) {
      setBusy(task.kind);
      setOperationTask({ operation: task.kind, startedAt: task.startedAt });
      task.promise
        .then(() => mounted && load())
        .catch((e) => mounted && setErr(e.message))
        .finally(() => mounted && setBusy(''));
    }
    return () => {
      mounted = false;
      loadRequestGate.current.invalidate();
      clearTimeout(pollRef.current);
    };
  }, [active, projectId, load]);
  useEffect(() => {
    if (!active) return undefined;
    const refresh = () => {
      setCandidateResult(null);
      void load();
    };
    window.addEventListener('mw-package-updated', refresh);
    return () => window.removeEventListener('mw-package-updated', refresh);
  }, [active, load]);

  const loadedTargetRound = latestDiagnosableRound(rounds ?? []);
  const loadedSuggestions = suggestions.filter((suggestion) => suggestion.roundId === loadedTargetRound?.id);
  const currentApplyTask = optimizationApplyTaskForSuggestions(applyTask, loadedSuggestions);
  const applyRunning = currentApplyTask?.node?.status === 'running';
  const applyNeedsExpert = currentApplyTask?.node?.status === 'waiting_user'
    && currentApplyTask?.node?.output?.phase === 'expert_review';
  const applyFailure = optimizationApplyFailureMessage(currentApplyTask);
  useEffect(() => {
    if (!applyRunning && busy !== 'apply') return undefined;
    let cancelled = false;
    const refresh = async () => {
      try {
        const data = await getOptimizeApplyStatus(projectId);
        if (!cancelled) setApplyTask(data.task);
      } catch (e) {
        if (!cancelled) setErr(e.message);
      }
    };
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => { cancelled = true; clearInterval(timer); };
  }, [projectId, applyRunning, busy]);

  const completedTaskId = useRef('');
  useEffect(() => {
    const node = applyTask?.node;
    if (!node || node.status !== 'done' || completedTaskId.current === node.id) return;
    completedTaskId.current = node.id;
    if (node.output?.candidateId) {
      showCandidate({
        candidateId: node.output.candidateId,
        selected: node.output.suggestionCount ?? 0,
      });
    }
    load();
  }, [applyTask?.node?.id, applyTask?.node?.status, load, showCandidate]);

  if (!rounds) return <div className="empty">{err || '加载中…'}</div>;
  const activeBusy = busy || (applyRunning ? 'apply' : applyNeedsExpert ? 'apply_review' : '');
  const targetRound = loadedTargetRound;
  const currentDiagnoses = diagnoses.filter((diagnosis) => diagnosis.roundId === targetRound?.id);
  const canPropose = diagnosisCanPropose;
  const hasExternalBlockers = currentDiagnoses.some((diagnosis) => diagnosis.blocking || !diagnosis.packageOptimizable);
  const currentSuggestions = loadedSuggestions;
  const pending = currentSuggestions.filter((s) => s.status === 'pending');
  const pendingIds = new Set(pending.map((suggestion) => suggestion.id));
  const selectedCurrent = new Set([...sel].filter((id) => pendingIds.has(id)));

  const propose = async () => {
    setBusy('propose'); setErr(''); setCandidateResult(null);
    setOperationTask({ operation: 'propose', startedAt: new Date().toISOString() });
    try {
      const d = await proposeOptimize(projectId, targetRound?.id);
      setSuggestions((prev) => {
        const known = new Set(prev.map((s) => s.id));
        return [...prev, ...d.suggestions.filter((s) => !known.has(s.id))];
      });
      setSel(new Set(d.suggestions.filter((s) => s.status === 'pending').map((s) => s.id)));
    } catch (e) { setErr(e.message); } finally { setBusy(''); setOperationTask(null); }
  };

  const apply = async () => {
    setBusy('apply'); setErr('');
    setOperationTask({ operation: 'apply', startedAt: new Date().toISOString() });
    try {
      const d = await applyOptimize(projectId, [...selectedCurrent]);
      if (d.status === 'waiting_expert') {
        const status = await getOptimizeApplyStatus(projectId);
        setApplyTask(status.task);
        setCandidateResult(null);
      } else {
        showCandidate(d);
      }
      await load();
      await onChanged?.();
    } catch (e) { setErr(e.message); } finally { setBusy(''); setOperationTask(null); }
  };

  const resolveExpertReview = async (action) => {
    const taskId = applyTask?.node?.id;
    if (!taskId) return;
    if (action === 'skip' && !window.confirm('确认跳过这些优化建议吗？它们将标记为已忽略，当前知识包不会被修改。')) return;
    setBusy('apply'); setErr('');
    try {
      const decision = await reviewOptimizeApply(projectId, taskId, action, expertNote);
      setExpertNote('');
      if (action === 'retry') {
        const d = await applyOptimize(projectId, decision.suggestionIds);
        if (d.status === 'waiting_expert') {
          const status = await getOptimizeApplyStatus(projectId);
          setApplyTask(status.task);
        } else {
          showCandidate(d);
        }
      }
      await load();
      await onChanged?.();
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const reject = async (id) => {
    if (busy || applyRunning) return;
    if (!window.confirm('确定忽略这条优化建议吗？忽略后不会再出现在待处理列表中。')) return;
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

  return (
    <div>
      {err && <div className="lerr">{err}</div>}
      <div className="ev-launch">
        {targetRound
          ? <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
              基于轮次：包 v{targetRound.packageVersion} · {targetRound.status === 'failed'
                ? '评测运行失败'
                : `通过 ${targetRound.passCount}/${targetRound.progressTotal}`}
            </div>
          : <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>请先运行评测</div>}
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>生成优化建议</div>
        <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
          只处理失败诊断中确认可通过知识包或脚本修复的问题。
        </div>
        <button className="btn btn-p" disabled={!!activeBusy || !targetRound || !canPropose}
          onClick={propose}>
          {activeBusy === 'propose' ? '生成中…' : '生成建议'}
        </button>
        {targetRound && !currentDiagnoses.length && <button className="btn" style={{ marginLeft: 10 }} onClick={onGoDiagnose}>先去失败诊断</button>}
        {!!currentDiagnoses.length && !canPropose && <span style={{ marginLeft: 10, fontSize: 12.5, color: 'var(--muted)' }}>暂无可改项</span>}
        {canPropose && hasExternalBlockers && (
          <div style={{ marginTop: 9, fontSize: 12.5, color: 'var(--warn, #9a6700)' }}>
            仍有数据、用例或运行问题。
          </div>
        )}
      </div>

      {activeBusy === 'propose' && operationTask && (
        <div style={{ marginTop: 12 }}><OptimizationOperationProgress task={operationTask} /></div>
      )}
      {activeBusy === 'apply' && !applyRunning && operationTask && (
        <div style={{ marginTop: 12 }}><OptimizationOperationProgress task={operationTask} /></div>
      )}
      {applyRunning && <div style={{ marginTop: 12 }}><OptimizationApplyProgress node={applyTask.node} /></div>}
      {applyNeedsExpert && (
        <div className="sg-applied" style={{ marginTop: 12, borderColor: 'var(--warn, #9a6700)' }}>
          <div style={{ fontWeight: 700, marginBottom: 7 }}>自动修复两次未通过，请专家决策</div>
          <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 8 }}>
            当前知识包没有被修改。可以根据下方原因补充意见后重试，也可确认跳过这些建议。
          </div>
          <div className="sg-q"><b>服务端校验原因</b><div>
            {(applyTask.node.output.validationErrors ?? [applyTask.node.output.error]).filter(Boolean)
              .map((item, index) => <div key={index}>• {item}</div>)}
          </div></div>
          <textarea value={expertNote} onChange={(event) => setExpertNote(event.target.value)}
            placeholder="可选：补充修复口径、目标资源或脚本职责边界"
            style={{ width: '100%', minHeight: 76, marginTop: 9 }} />
          <div style={{ display: 'flex', gap: 8, marginTop: 9 }}>
            <button className="btn btn-p" disabled={!!busy} onClick={() => resolveExpertReview('retry')}>
              带反馈重试
            </button>
            <button className="btn" disabled={!!busy} onClick={() => resolveExpertReview('skip')}>
              确认跳过
            </button>
          </div>
        </div>
      )}
      {applyFailure && (
        <div className="lerr" style={{ marginTop: 12 }}>
          <strong>优化候选生成失败：</strong>{applyFailure}
          <div style={{ marginTop: 6, fontWeight: 400 }}>
            当前知识包没有被修改。专家可以调整后重试，或忽略不适用的建议后继续发布。
          </div>
        </div>
      )}

      {/* 建议列表 */}
      {currentSuggestions.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="sg-bar">
            <span className="sg-bar-t">优化建议</span>
            <span className="sg-bar-c">{pending.length > 0 ? `已选 ${selectedCurrent.size}/${pending.length}` : '已处理'}</span>
            {pending.length > 0 && (
              <span className="sg-bar-ops">
                <button onClick={() => setSel(new Set(pending.map((s) => s.id)))}>全选</button>
                <i>·</i>
                <button onClick={() => setSel(new Set())}>清空</button>
              </span>
            )}
          </div>
          {currentSuggestions.map((s) => (
            <SuggestionCard key={s.id} s={s} checked={selectedCurrent.has(s.id)}
              onToggle={() => toggle(s.id)} onReject={() => reject(s.id)} />
          ))}
          {pending.length > 0 && (
            <button className="btn btn-p" style={{ marginTop: 8 }} disabled={!!activeBusy || !selectedCurrent.size} onClick={apply}>
              {activeBusy === 'apply' ? '生成候选中…' : `生成候选修订（${selectedCurrent.size}）`}
            </button>
          )}
        </div>
      )}

      {candidateResult && (
        <div className="sg-applied">
          已生成待确认的修改（包含 {candidateResult.selected ?? 0} 条建议）。
          请在右侧“候选修订”卡片查看差异并确认；确认前不会改变当前知识包。
        </div>
      )}

      {/* 版本历史与交付 */}
      {versions.length > 0 && (
        <div style={{ marginTop: 22 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>版本</div>
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
      const res = await authFetch(exportVersionUrl(projectId, version));
      if (!res.ok) { setDlErr(`导出失败（${res.status}）`); return; }
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]) || `package-${version}.zip`;
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(a.href), 1000);
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
              <td><button
                className="btn"
                style={{ padding: '3px 10px', fontSize: 11.5 }}
                disabled={Boolean(v.unversioned)}
                title={v.unversioned ? '请先把当前内容发布为正式版本' : '导出该版本'}
                onClick={() => download(v.version)}
              >导出</button></td>
            </tr>
          );
        })}
      </tbody>
    </table>
    </>
  );
}
