// 已发布版本的独立评测（ADR-0092，ES-01～ES-08）：照 develop 的 PublishedVersionsDialog 与 PackageEvaluationModals。
// 发布页的发布记录表这样用：
//   const ev = useReleaseEvaluations(pid, revision);  // revision 变了就重读；发布成功调 notifyPackagePublished(pid) 也会重读
//   卡片顶部：<EvaluationHint total={记录数} />，<EvaluationNotices ev={ev} />
//   「评测」列：<EvaluationCell ev={ev} release={row} />（row 要有 release_id、version）
//   ev.hasSolver(rows) 时「求解验证」列：<SolverValidationCell ev={ev} release={row} />
//   ev.hasFeedback 时「用户反馈」列：<FeedbackCell ev={ev} release={row} />
//   操作列：<StartEvaluation ev={ev} release={row} />
//   页面任意处：<EvaluationDialogs ev={ev} />
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { Area, Button, Chip, Modal, Note } from '../../ui/index.jsx';
import { useProjectEvent } from '../plugin/parts.jsx';
import { ArtifactBody, message, useAnswerPreview, useLoad } from './EvaluationDoc.jsx';
import { openEvaluation, prefetchEvaluationEntry } from './evaluationEntry.js';
import {
  EVALUATION_HINT, PACKAGE_PUBLISHED, POLL_INTERVAL_MS, RECONCILE_EVERY, SOLVER_VALIDATION_STATES, canReportFailure, hasFeedback,
  mergeStatus, needsPolling, processingState, releaseEvaluation, releaseFeedback, releaseSolverValidation,
  writebackNotice,
} from './evaluationModel.js';
import '../../styles/evaluation.css';

export { LegacyEvaluations } from './LegacyEvaluations.jsx';
export { SolverValidationCard } from './SolverValidationCard.jsx';

const REPORT_LIMIT_HINT = '报告较长，仅显示前 2 MiB';
// develop 的 antd 颜色 → 设计系统的标签色
export const TONES = {
  gold: 'amber', green: 'teal', red: 'red', processing: 'blue', default: 'gray',
  warning: 'amber', success: 'teal', error: 'red',
};
// 报告与建议篇幅长：弹窗尽量放大，同时在视口左右各留出边距
const WIDE = 'min(1280px, calc(100vw - 96px))';

export function useReleaseEvaluations(pid, revision = 0) {
  const { api, services } = useApp();
  const [status, setStatus] = useState(null);
  const [refreshes, setRefreshes] = useState(0);
  const [dialog, setDialog] = useState(null);
  const pending = useRef(false);
  const refresh = useCallback(() => setRefreshes((n) => n + 1), []);

  useEffect(() => { setStatus(null); setDialog(null); }, [pid]);
  // 打开时向评测服务对账一次；仍有评测或产物在进行时轮询本地索引，并定期再对账
  useEffect(() => {
    if (!pid) return undefined;
    let alive = true;
    api.evaluations(pid, { reconcile: true })
      .then((r) => { if (alive) setStatus(r); })
      .catch((e) => { if (alive) setStatus({ configured: false, message: `评测状态读取失败：${message(e)}`, releases: {} }); });
    return () => { alive = false; };
  }, [api, pid, revision, refreshes]);
  const polling = !!pid && needsPolling(status);
  useEffect(() => {
    if (!polling) return undefined;
    let alive = true;
    let count = 0;
    const timer = setInterval(() => {
      if (pending.current) return;
      count += 1;
      pending.current = true;
      api.evaluations(pid, { reconcile: count % RECONCILE_EVERY === 0 })
        .then((r) => { if (alive) setStatus((prev) => mergeStatus(prev, r)); })
        .catch(() => {})
        .finally(() => { pending.current = false; });
    }, POLL_INTERVAL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [api, pid, polling]);
  // 从别处发布：发布入口经窗口事件通知（develop 的 notifyPackagePublished），项目流的 release.updated 同样重读
  useEffect(() => {
    const onPublished = (event) => { if (event.detail?.projectId === pid) refresh(); };
    window.addEventListener(PACKAGE_PUBLISHED, onPublished);
    return () => window.removeEventListener(PACKAGE_PUBLISHED, onPublished);
  }, [pid, refresh]);
  useProjectEvent('release.updated', refresh);
  // 失败不阻断：点击「评测」时会重新读取并给出原因
  useEffect(() => { if (pid) prefetchEvaluationEntry(api.evaluationEntry).catch(() => {}); }, [api, pid]);

  const evaluate = useCallback((release, runType = '') => {
    openEvaluation({ ontology_id: status?.ontology_id, ...release }, {
      loadEntry: api.evaluationEntry,
      tokens: () => ({ access_token: services.tokens.access, refresh_token: services.tokens.refresh }),
      runType,
    }).catch((e) => toast(message(e), 'alert'));
  }, [api, services, status?.ontology_id]);

  return {
    pid, status, refresh, evaluate, dialog, setDialog,
    enabled: !!status?.configured,
    hasFeedback: hasFeedback(status),
    hasSolver: (rows) => (rows || []).some((r) => releaseSolverValidation(status, r.release_id)),
  };
}

// 不可用的按钮不消失，置灰并悬停说明原因
export function ReasonLink({ reason, children, onClick }) {
  if (reason) return <span className="ev-link is-disabled" data-pop={reason} aria-disabled="true">{children}</span>;
  return <span className="ev-link" role="button" tabIndex={0} onClick={onClick} onKeyDown={(e) => e.key === 'Enter' && onClick()}>{children}</span>;
}

export function EvaluationHint({ total }) {
  return <div className="sm muted mb8">共 {total} 个版本。{EVALUATION_HINT}</div>;
}

export function EvaluationNotices({ ev }) {
  const notice = writebackNotice(ev.status);
  return (
    <>
      {ev.status?.reachable === false ? <Note tone="amber" className="mb8">评测服务暂不可用，评测状态可能不是最新</Note> : null}
      {notice ? (
        <Note tone={notice.type === 'warning' ? 'amber' : undefined} className="mb8">
          <span className="row">
            <span className="grow">{notice.text}</span>
            {notice.type === 'warning' ? <Button size="xs" kind="ghost" onClick={ev.refresh}>重试</Button> : null}
          </span>
        </Note>
      ) : null}
    </>
  );
}

// 版本的一种来源（评测 / 用户反馈）占一格：处理状态加上报告和优化建议两个链接
function SourceCell({ ev, release, source, feedback, empty }) {
  const target = source.entry && { runId: source.entry.run_id, version: release.version, releaseId: release.release_id, entry: source.entry, feedback };
  if (!target) return <span className="xs muted">{empty}</span>;
  const state = processingState(source.entry);
  return (
    <span className="ev-cell">
      {state ? <Chip sm tone={TONES[state.color] || 'gray'} data-pop={state.hint}>{state.label}</Chip> : null}
      <ReasonLink reason={source.report.reason} onClick={() => ev.setDialog({ kind: 'report', target })}>报告</ReasonLink>
      <ReasonLink reason={source.suggestions.reason} onClick={() => ev.setDialog({ kind: 'suggestions', target })}>优化建议</ReasonLink>
    </span>
  );
}

export function EvaluationCell({ ev, release }) {
  const source = releaseEvaluation(ev.status, release.release_id);
  return <SourceCell ev={ev} release={release} source={source} empty={<span data-pop={source.report.reason}>{source.idle}</span>} />;
}

export function FeedbackCell({ ev, release }) {
  return <SourceCell ev={ev} release={release} source={releaseFeedback(ev.status, release.release_id)} feedback empty="—" />;
}

// 求解验证在发布后的独立评测中进行：只有发布时启用了求解契约、或已有验证记录的版本显示
export function SolverValidationCell({ ev, release }) {
  const validation = releaseSolverValidation(ev.status, release.release_id);
  if (!validation) return <span className="xs muted">—</span>;
  const view = SOLVER_VALIDATION_STATES[validation.state];
  const target = validation.entry && { runId: validation.entry.run_id, version: release.version, releaseId: release.release_id };
  return (
    <span className="ev-cell">
      <Chip sm tone={TONES[view.color]} data-pop={validation.reason}>{view.label}</Chip>
      {target && validation.state !== 'running' ? <ReasonLink onClick={() => ev.setDialog({ kind: 'report', target })}>验证报告</ReasonLink> : null}
      {['unverified', 'failed'].includes(validation.state) ? <ReasonLink onClick={() => ev.evaluate(release, 'solver')}>去验证</ReasonLink> : null}
    </span>
  );
}

export function StartEvaluation({ ev, release }) {
  return <ReasonLink onClick={() => ev.evaluate(release)}>发起评测</ReasonLink>;
}

export function ReportModal({ ev, target, onClose }) {
  const { api } = useApp();
  const state = useLoad(() => api.evaluationReport(ev.pid, target.runId), target.runId);
  const preview = useAnswerPreview((resultId) => api.evaluationAnswer(ev.pid, target.runId, resultId));
  return (
    <Modal open onClose={onClose} w={WIDE} title={`${target.feedback ? '用户反馈报告' : '评测报告'} — 版本 ${target.version || ''}`}
      foot={<><span className="sp" /><Button onClick={onClose}>关闭</Button></>}>
      {state.data?.truncated ? <Note className="mb8">{REPORT_LIMIT_HINT}</Note> : null}
      <ArtifactBody state={state} onOpenAnswer={preview.open} />
      {preview.modal}
    </Modal>
  );
}

// 数据飞轮也从这里打开：onOptimize 给出时底部多一个「评测优化」，blockReason 是它置灰的原因
export function SuggestionsModal({
  ev, target, onClose, onReportFailure, onOptimize, blockReason, onOptimized,
}) {
  const { api } = useApp();
  const state = useLoad(() => api.evaluationSuggestions(ev.pid, target.runId), target.runId);
  const preview = useAnswerPreview((resultId) => api.evaluationAnswer(ev.pid, target.runId, resultId));
  const [regenerating, setRegenerating] = useState(false);
  const [optimizing, setOptimizing] = useState(false);
  const failed = state.data?.status === 'failed';

  const regenerate = async () => {
    setRegenerating(true);
    try {
      await api.regenerateEvaluation(ev.pid, target.runId);
      toast('已开始重新生成评测报告和优化建议');
      onClose();
      ev.refresh();
    } catch (e) {
      toast(message(e), 'alert');
    } finally {
      setRegenerating(false);
    }
  };
  const optimize = async () => {
    setOptimizing(true);
    try {
      await onOptimize();
      toast('已导入评测报告和优化建议，开始评测优化');
      onOptimized?.();
    } catch (e) {
      toast(message(e), 'alert');
    } finally {
      setOptimizing(false);
    }
  };
  return (
    <Modal open onClose={onClose} w={WIDE} title={`${target.feedback ? '用户反馈优化建议' : '优化建议'} — 版本 ${target.version || ''}`}
      foot={(
        <>
          <Button onClick={onClose}>关闭</Button>
          <span className="sp" />
          {onReportFailure ? <Button onClick={onReportFailure}>标记处理失败</Button> : null}
          {onOptimize ? (
            <Button kind="primary" icon="wand" disabled={!!blockReason || optimizing} title={blockReason || undefined} onClick={optimize}>
              评测优化
            </Button>
          ) : null}
        </>
      )}>
      {failed ? (
        <Note tone="red" className="mb8">
          <span className="row">
            <span className="grow"><b>优化建议生成失败</b>{state.data.error ? <div>{state.data.error}</div> : null}</span>
            <Button size="xs" kind="ghost" disabled={regenerating} onClick={regenerate}>重新生成</Button>
          </span>
        </Note>
      ) : null}
      <ArtifactBody state={state} onOpenAnswer={preview.open} />
      {preview.modal}
    </Modal>
  );
}

// 按建议修改后无法发布新版本时，告诉评测服务这一轮处理失败及原因；成功的处理由发布新版本自动回写
function FailureModal({ ev, target, onClose, onReported }) {
  const { api } = useApp();
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    setSaving(true);
    try {
      await api.reportEvaluationFailure(ev.pid, target.runId, note.trim());
      toast('已回写评测服务：这一轮优化建议处理失败');
      onReported();
    } catch (e) {
      toast(message(e), 'alert');
    } finally {
      setSaving(false);
    }
  };
  return (
    <Modal open onClose={onClose} w={560} title={`标记处理失败 — 版本 ${target.version || ''}`}
      foot={<><span className="sp" /><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={!note.trim() || saving} onClick={submit}>提交</Button></>}>
      <p className="sm muted mb8">建议无法落实、发布不了新版本时使用。评测服务会记录“处理失败”和原因；之后修改并发布新版本，仍会自动变为“已处理”。</p>
      <Area rows={4} maxLength={1000} value={note} placeholder="例如：建议要求的数据表在业务库中不存在" onChange={(e) => setNote(e.target.value)} autoFocus />
      <div className="xs muted ev-count">{note.length} / 1000</div>
    </Modal>
  );
}

export function EvaluationDialogs({ ev }) {
  const { dialog, setDialog } = ev;
  if (!dialog) return null;
  const close = () => setDialog(null);
  const { target } = dialog;
  if (dialog.kind === 'report') return <ReportModal ev={ev} target={target} onClose={close} />;
  if (dialog.kind === 'failure') {
    return <FailureModal ev={ev} target={target} onClose={() => setDialog({ kind: 'suggestions', target })} onReported={() => { close(); ev.refresh(); }} />;
  }
  const failure = canReportFailure(ev.status, target.releaseId, target.entry) ? () => setDialog({ kind: 'failure', target }) : null;
  return <SuggestionsModal ev={ev} target={target} onClose={() => { close(); }} onReportFailure={failure} />;
}
