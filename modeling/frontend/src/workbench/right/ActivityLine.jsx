import { useEffect, useMemo, useState } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { elapsedText } from '../lib/mock/stream.js';
import Icon from '../ui/Icon.jsx';
import { userFacingText } from '../lib/userFacingText.js';

const selAct = (s) => s.activity;
const selOpen = (s) => s.activityOpen;
const selRun = (s) => s.run;
const selInteraction = (s) => s.interaction;
const selTrace = (s) => s.trace;
const selStreaming = (s) => s.messages.some((message) => message.role === 'assistant' && message.streaming);

export function executionStartedAt(run, now = Date.now()) {
  const updatedAt = Date.parse(String(run?.updatedAt || ''));
  return Number.isFinite(updatedAt) && updatedAt > 0 && updatedAt <= now ? updatedAt : undefined;
}

export function traceRunActivity(trace, run, executionStart) {
  const runId = String(run?.id || '');
  const latest = (Array.isArray(trace) ? trace : [])
    .filter((item) => !runId || !item?.runId || item.runId === runId)
    .reduce((current, item) => Number(item?.seq || 0) >= Number(current?.seq || 0) ? item : current, null);
  if (!latest) return null;
  const observedAt = Date.parse(String(latest.createdAt || ''));
  if (executionStart !== undefined
    && (!Number.isFinite(observedAt) || observedAt < executionStart - 1_000)) return null;
  const active = ['started', 'repairing'].includes(String(latest.phase || ''));
  return {
    id: `trace:${latest.id || latest.seq || latest.toolCallId || 'current'}`,
    text: latest.summary || '助手正在处理当前步骤',
    detail: active ? '运行轨迹正在更新，你可以继续输入' : '当前动作已结束，正在继续下一步',
    progress: {},
    sub: [],
    startedAt: Number.isFinite(observedAt) ? observedAt : executionStart,
    observedAt: Number.isFinite(observedAt) ? observedAt : undefined,
  };
}

export function visibleRunActivity({ activity, run, interaction, streaming, trace }, now = Date.now()) {
  const status = String(run?.status || '');
  const active = ['queued', 'running'].includes(status);
  if (interaction && !active) return null;
  if (status && !['queued', 'running'].includes(status)) return null;
  const executionStart = executionStartedAt(run, now);
  if (activity) {
    const observedAt = Number(activity.observedAt);
    if (executionStart === undefined
      || (Number.isFinite(observedAt) && observedAt >= executionStart - 1_000)) return activity;
  }
  if (!['queued', 'running'].includes(status)) return null;
  const traceActivity = traceRunActivity(trace, run, executionStart);
  if (traceActivity) return traceActivity;
  return {
    id: `run:${run?.id || 'active'}`,
    text: status === 'queued'
      ? '任务已提交，等待开始'
      : streaming ? '助手正在生成回复' : '助手正在处理当前步骤',
    detail: status === 'queued'
      ? '任务会在服务端开始，你可以继续输入或离开页面'
      : streaming ? '内容正在实时输出，你可以随时插话' : '正在等待下一项进度，你可以继续输入',
    progress: {},
    sub: [],
    startedAt: executionStart,
  };
}

/* 三级递进的第一级。不进历史，任务结束后折叠成一条正常消息。
   两条硬要求：说人话（用 display 文案，不显示工具名）、带耗时。 */
export default function ActivityLine() {
  const activity = useStore(store, selAct);
  const open = useStore(store, selOpen);
  const run = useStore(store, selRun);
  const interaction = useStore(store, selInteraction);
  const trace = useStore(store, selTrace);
  const streaming = useStore(store, selStreaming);
  const act = useMemo(() => visibleRunActivity({ activity, run, interaction, streaming, trace }), [
    activity, interaction, run, streaming, trace,
  ]);
  const [, tick] = useState(0);

  // 只在有活动时起秒表，空闲时不跑定时器
  useEffect(() => {
    if (!act) return undefined;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [act]);

  if (!act) return null;
  const progress = Array.isArray(act.progress)
    ? { current: act.progress[0], total: act.progress[1] }
    : act.progress || {};
  const current = Number(progress.current) || 0;
  const total = Number(progress.total) || 0;
  const rawPercent = Number(progress.percent);
  const percent = Number.isFinite(rawPercent) ? Math.max(0, Math.min(100, rawPercent)) : undefined;
  const reportedElapsed = Number(progress.elapsedSeconds);
  const hasReportedElapsed = progress.elapsedSeconds !== undefined && progress.elapsedSeconds !== null
    && Number.isFinite(reportedElapsed) && reportedElapsed >= 0;
  const startedAt = Number(act.startedAt);
  const hasStartedAt = Number.isFinite(startedAt) && startedAt > 0 && startedAt <= Date.now();
  const elapsedSeconds = hasReportedElapsed || hasStartedAt
    ? Math.max(
      hasReportedElapsed ? reportedElapsed : 0,
      hasStartedAt ? Math.floor((Date.now() - startedAt) / 1000) : 0,
    )
    : null;
  const detail = act.detail || ((elapsedSeconds ?? 0) >= 120
    ? '这一步等待时间较长，你可以继续输入或停止任务'
    : (elapsedSeconds ?? 0) >= 30 ? '仍在等待下一项进度，你可以继续输入或离开页面' : '正在处理当前步骤');
  const progressText = total > 0
    ? `${current}/${total} ${progress.unit || '项'}`
    : percent !== undefined ? `${Math.round(percent)}%` : '';
  const sub = Array.isArray(act.sub) ? act.sub : [];
  const activityText = userFacingText(act.text);
  const canCancel = (elapsedSeconds ?? 0) >= 120 && ['queued', 'running'].includes(String(run?.status || ''));

  return (
    <div className="wb-act">
      <button className="row" onClick={actions.toggleActivity} aria-expanded={open} title={activityText}>
        <span className="sp" aria-hidden="true" />
        <span className="tx">
          {activityText}
        </span>
        {elapsedSeconds !== null && <span className="el">{elapsedText(Date.now() - elapsedSeconds * 1000, Date.now())}</span>}
        <Icon name={open ? 'chevronUp' : 'chevron'} size={13} style={{ color: 'var(--c-muted)' }} />
      </button>
      <div className="wb-act-status">
        <div className={`wb-act-meter${percent === undefined ? ' indeterminate' : ''}`}
          role="progressbar" aria-label={activityText}
          aria-valuemin="0" aria-valuemax="100"
          aria-valuetext={progressText || userFacingText(detail)}
          {...(percent !== undefined ? { 'aria-valuenow': Math.round(percent) } : {})}>
          <i style={percent !== undefined ? { width: `${percent}%` } : undefined} />
        </div>
        <div className="wb-act-meta" aria-live="polite">
          <span>{userFacingText(detail)}</span>
          {progressText && <b>{progressText}</b>}
          {canCancel && (
            <button type="button" className="wb-act-stop" onClick={() => void actions.cancel()}>
              <Icon name="x" size={12} />停止任务
            </button>
          )}
        </div>
      </div>
      {open && (
        <div className="sub">
          {sub.map((s) => <div key={s}>{userFacingText(s)}</div>)}
          <button type="button" className="more" onClick={actions.toggleDrawer}>查看全部处理记录 →</button>
        </div>
      )}
    </div>
  );
}
