// 已发布版本的独立评测（ADR-0092，EV-01～EV-11）：版本行里的评测、求解验证、用户反馈三格，表格上方的提示，
// 报告、优化建议、完整回答、标记处理失败的弹窗，以及旧版评测记录（只读）。
// 挂在发布页的已发布版本表上，用法：
//   const ev = useReleaseEvaluations(pid, revision);  // revision 变了就重读（发布、回退后）
//   <EvaluationNotices ev={ev} />  表格上方
//   ev.enabled 时加一列「评测」：<EvaluationCell ev={ev} release={row} />（row 要有 release_id、version）
//   ev.hasSolver(rows) 时加一列「求解验证」：<SolverValidationCell ev={ev} release={row} />
//   ev.hasFeedback 时加一列「用户反馈」：<FeedbackCell ev={ev} release={row} />
//   <EvaluationDialogs ev={ev} tasks={tasks} />  tasks 是页面的 useCommandTasks，评测优化经它发命令
//   <LegacyEvaluations pid={pid} />  页头操作区；没有旧记录时什么都不画
import { useCallback, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Area, Button, Chip, Drawer, Modal, Note } from '../../ui/index.jsx';
import { useProjectEvent } from '../plugin/parts.jsx';
import { openEvaluation, prefetchEvaluationEntry } from './evaluationEntry.js';
import {
  POLL_INTERVAL_MS, RECONCILE_EVERY, SOLVER_VALIDATION_STATES, canReportFailure, hasFeedback, mergeStatus, needsPolling,
  optimizationBlock, processingState, releaseEvaluation, releaseFeedback, releaseSolverValidation, writebackNotice,
} from './evaluationModel.js';
import '../../styles/evaluation.css';

const TONES = { gold: 'amber', green: 'teal', red: 'red', processing: 'blue', default: 'gray' };
const SOLVER_TONES = { warning: 'amber', processing: 'blue', success: 'teal', error: 'red' };
// 报告里「完整回答」的链接：reports/<报告>.answers.json#<结果 ID>
const ANSWER_LINK = /\.answers\.json#(.+)$/;

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
      .catch((e) => { if (alive) setStatus({ configured: false, message: errorText(e), releases: {} }); });
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
  useProjectEvent('release.updated', refresh);
  const enabled = !!status?.configured;
  // 点「去评测」要在任何 await 之前打开窗口：先取好入口地址，失败时点击会再取并给出原因
  useEffect(() => { if (enabled) prefetchEvaluationEntry(api.evaluationEntry).catch(() => {}); }, [api, enabled]);

  const evaluate = useCallback((release, runType = '') => {
    openEvaluation({ ontology_id: status?.ontology_id, ...release }, {
      loadEntry: api.evaluationEntry,
      tokens: () => ({ access_token: services.tokens.access, refresh_token: services.tokens.refresh }),
      runType,
    }).catch((e) => toast(errorText(e), 'alert'));
  }, [api, services, status?.ontology_id]);

  return {
    pid, status, enabled, refresh, evaluate, dialog, setDialog,
    hasFeedback: enabled && hasFeedback(status),
    hasSolver: (rows) => enabled && (rows || []).some((r) => releaseSolverValidation(status, r.release_id)),
  };
}

function Link({ children, onClick }) {
  return <span className="ev-link" role="button" tabIndex={0} onClick={onClick} onKeyDown={(e) => e.key === 'Enter' && onClick()}>{children}</span>;
}

// 一种来源（评测 / 用户反馈）占一格：处理状态，加上能看的报告和优化建议
function SourceCell({ ev, release, source, feedback, idle, children }) {
  if (!source.entry) {
    return <span className="ev-cell">{idle ? <span className="xs muted">{idle}</span> : null}{children}</span>;
  }
  const target = { runId: source.entry.run_id, version: release.version, releaseId: release.release_id, entry: source.entry, feedback };
  const state = processingState(source.entry);
  return (
    <span className="ev-cell">
      {state ? <Chip sm tone={TONES[state.color] || 'gray'} data-pop={state.hint}>{state.label}</Chip> : null}
      {source.report.disabled ? null : <Link onClick={() => ev.setDialog({ kind: 'report', target })}>报告</Link>}
      {source.suggestions.disabled ? null : <Link onClick={() => ev.setDialog({ kind: 'suggestions', target })}>优化建议</Link>}
      {children}
    </span>
  );
}

export function EvaluationCell({ ev, release }) {
  const source = releaseEvaluation(ev.status, release.release_id);
  const running = source.idle === '评测中';
  return (
    <SourceCell ev={ev} release={release} source={source} idle={source.idle === '未评测' ? '' : source.idle}>
      {running ? null : <Link onClick={() => ev.evaluate(release)}>去评测</Link>}
    </SourceCell>
  );
}

export function FeedbackCell({ ev, release }) {
  return <SourceCell ev={ev} release={release} source={releaseFeedback(ev.status, release.release_id)} feedback idle="—" />;
}

// 求解验证在发布后的独立评测里做：只有发布时启用了求解契约、或已有验证记录的版本有内容
export function SolverValidationCell({ ev, release }) {
  const validation = releaseSolverValidation(ev.status, release.release_id);
  if (!validation) return <span className="xs muted">—</span>;
  const view = SOLVER_VALIDATION_STATES[validation.state];
  const target = validation.entry && { runId: validation.entry.run_id, version: release.version, releaseId: release.release_id };
  return (
    <span className="ev-cell">
      <Chip sm tone={SOLVER_TONES[view.color]} data-pop={validation.reason}>{view.label}</Chip>
      {target && validation.state !== 'running' ? <Link onClick={() => ev.setDialog({ kind: 'report', target })}>报告</Link> : null}
      {['unverified', 'failed'].includes(validation.state) ? <Link onClick={() => ev.evaluate(release, 'solver')}>去验证</Link> : null}
    </span>
  );
}

export function EvaluationNotices({ ev }) {
  if (!ev.enabled) return null;
  const notice = writebackNotice(ev.status);
  return (
    <>
      {ev.status?.reachable === false ? <Note tone="amber" className="mb8">评测服务连不上，显示的是上次同步的状态</Note> : null}
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

// ---------- 报告正文 ----------

function Doc({ content, onAnswer }) {
  const components = {
    a: ({ href, children }) => {
      const answer = (href || '').match(ANSWER_LINK);
      if (answer && onAnswer) return <Link onClick={() => onAnswer(decodeURIComponent(answer[1]))}>{children}</Link>;
      if (/^https?:\/\//.test(href || '')) return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
      return <span>{children}</span>; // 工作区路径在这里打不开
    },
  };
  return <div className="ev-doc"><Markdown remarkPlugins={[remarkGfm]} components={components}>{content}</Markdown></div>;
}

function useLoad(load, key) {
  const [state, setState] = useState({ loading: true, error: '', data: null });
  const [round, setRound] = useState(0);
  useEffect(() => {
    if (!key) return undefined;
    let alive = true;
    setState({ loading: true, error: '', data: null });
    load()
      .then((data) => { if (alive) setState({ loading: false, error: '', data }); })
      .catch((e) => { if (alive) setState({ loading: false, error: errorText(e), data: null }); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, round]);
  return { ...state, retry: () => setRound((n) => n + 1) };
}

function Loaded({ state, children }) {
  if (state.loading) return <div className="sm muted"><span className="spin" />读取中…</div>;
  if (state.error) {
    return <Note tone="red"><span className="row"><span className="grow">{state.error}</span><Button size="xs" kind="ghost" onClick={state.retry}>重试</Button></span></Note>;
  }
  return children;
}

function AnswerModal({ answer, onClose }) {
  return (
    <Modal open={!!answer} onClose={onClose} title="完整回答" sub={answer ? `${answer.originalLength} 字符${answer.truncated ? `，只保留前 ${answer.fullAnswer.length} 字符` : ''}` : ''} w={860}>
      {answer ? <Doc content={answer.fullAnswer} /> : null}
    </Modal>
  );
}

function useAnswer(read) {
  const [answer, setAnswer] = useState(null);
  const open = (resultId) => read(resultId).then(setAnswer).catch((e) => toast(errorText(e), 'alert'));
  return { open, modal: <AnswerModal answer={answer} onClose={() => setAnswer(null)} /> };
}

const WIDE = 'min(1180px, calc(100vw - 96px))';

function ReportModal({ ev, target, onClose }) {
  const { api } = useApp();
  const state = useLoad(() => api.evaluationReport(ev.pid, target.runId), target.runId);
  const answer = useAnswer((resultId) => api.evaluationAnswer(ev.pid, target.runId, resultId));
  return (
    <Modal open onClose={onClose} title={`${target.feedback ? '用户反馈报告' : '评测报告'} · ${target.version}`} w={WIDE}>
      <Loaded state={state}>
        {state.data?.truncated ? <Note className="mb8">只显示前 2 MiB</Note> : null}
        <Doc content={state.data?.content || ''} onAnswer={answer.open} />
      </Loaded>
      {answer.modal}
    </Modal>
  );
}

function SuggestionsModal({ ev, target, tasks, onClose, onFailure }) {
  const { api } = useApp();
  const state = useLoad(() => api.evaluationSuggestions(ev.pid, target.runId), target.runId);
  const answer = useAnswer((resultId) => api.evaluationAnswer(ev.pid, target.runId, resultId));
  const [busy, setBusy] = useState(false);
  const blocked = optimizationBlock(ev.status, target.releaseId, !!tasks);
  const act = async (fn) => {
    setBusy(true);
    try { await fn(); } catch (e) { toast(errorText(e), 'alert'); } finally { setBusy(false); }
  };
  const regenerate = () => act(async () => {
    await api.regenerateEvaluation(ev.pid, target.runId);
    toast('已开始重新生成');
    onClose();
    ev.refresh();
  });
  // 评测优化：产物整理成一批外部反馈，交给反馈演进在工作台对话里处理
  const optimize = () => act(async () => {
    const r = await api.optimizeEvaluation(ev.pid, target.runId);
    onClose();
    await tasks.send({ command: r.command, args: r.args, text: `评测优化 · ${target.version}` });
  });
  const failed = state.data?.status === 'failed';
  const foot = blocked && !onFailure ? null : (
    <>
      {onFailure ? <Button disabled={busy} onClick={onFailure}>标记处理失败</Button> : null}
      <span className="sp" />
      {blocked ? null : <Button kind="primary" icon="sparkle" disabled={busy || tasks.sending} onClick={optimize}>评测优化</Button>}
    </>
  );
  return (
    <Modal open onClose={onClose} title={`${target.feedback ? '用户反馈优化建议' : '优化建议'} · ${target.version}`} w={WIDE} foot={foot}>
      <Loaded state={state}>
        {failed ? (
          <Note tone="red" className="mb8">
            <span className="row">
              <span className="grow">优化建议生成失败{state.data.error ? `：${state.data.error}` : ''}</span>
              <Button size="xs" kind="ghost" disabled={busy} onClick={regenerate}>重新生成</Button>
            </span>
          </Note>
        ) : null}
        {state.data?.content ? <Doc content={state.data.content} onAnswer={answer.open} /> : null}
      </Loaded>
      {answer.modal}
    </Modal>
  );
}

// 按建议改了却发布不了新版本时，告诉评测服务这一轮处理失败；处理成功由发布新版本自动回写
function FailureModal({ ev, target, onClose }) {
  const { api } = useApp();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await api.reportEvaluationFailure(ev.pid, target.runId, note.trim());
      toast('已回写评测服务');
      onClose();
      ev.refresh();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={onClose} title={`标记处理失败 · ${target.version}`} w={560}
      foot={<><span className="sp" /><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={busy || !note.trim()} onClick={submit}>提交</Button></>}>
      <Area minHeight={110} maxLength={1000} value={note} placeholder="失败原因" onChange={(e) => setNote(e.target.value)} autoFocus />
    </Modal>
  );
}

export function EvaluationDialogs({ ev, tasks }) {
  const { dialog, setDialog } = ev;
  if (!dialog) return null;
  const close = () => setDialog(null);
  const { target } = dialog;
  if (dialog.kind === 'report') return <ReportModal ev={ev} target={target} onClose={close} />;
  if (dialog.kind === 'failure') return <FailureModal ev={ev} target={target} onClose={close} />;
  const failure = canReportFailure(ev.status, target.releaseId, target.entry) ? () => setDialog({ kind: 'failure', target }) : null;
  return <SuggestionsModal ev={ev} target={target} tasks={tasks} onClose={close} onFailure={failure} />;
}

// ---------- 旧版评测记录（只读） ----------

function LegacyReport({ pid, stem, onBack }) {
  const { api } = useApp();
  const state = useLoad(() => api.legacyEvaluation(pid, stem), stem);
  const answer = useAnswer((resultId) => api.legacyEvaluationAnswer(pid, stem, resultId));
  return (
    <>
      <Button size="sm" kind="ghost" icon="arrowl" className="mb8" onClick={onBack}>返回</Button>
      <Loaded state={state}>
        {state.data?.truncated ? <Note className="mb8">只显示前 2 MiB</Note> : null}
        <Doc content={state.data?.content || ''} onAnswer={answer.open} />
      </Loaded>
      {answer.modal}
    </>
  );
}

export function LegacyEvaluations({ pid }) {
  const { api } = useApp();
  const [items, setItems] = useState([]);
  const [open, setOpen] = useState(false);
  const [stem, setStem] = useState('');
  useEffect(() => {
    let alive = true;
    setItems([]);
    if (pid) api.legacyEvaluations(pid).then((r) => { if (alive) setItems(r.items || []); }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid]);
  if (!items.length) return null;
  return (
    <>
      <Button kind="ghost" icon="history" onClick={() => setOpen(true)}>旧版评测记录</Button>
      <Drawer open={open} onClose={() => { setOpen(false); setStem(''); }} icon="history" title="旧版评测记录" w={stem ? 920 : 520}>
        {stem ? <LegacyReport pid={pid} stem={stem} onBack={() => setStem('')} /> : items.map((item) => (
          <div key={item.id} className="ev-legacy-row" role="button" tabIndex={0} onClick={() => setStem(item.id)} onKeyDown={(e) => e.key === 'Enter' && setStem(item.id)}>
            <span className="grow">{item.title}</span>
            {item.score == null ? null : <Chip sm tone="blue">{item.score} 分</Chip>}
          </div>
        ))}
      </Drawer>
    </>
  );
}
