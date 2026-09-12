import { useMemo } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
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

function isPostToolPlanningActivity(activity) {
  return activity?.text === '正在根据处理结果判断下一步'
    && (String(activity?.phase || '') === 'planning'
      || String(activity?.id || '').startsWith('after-tool:'));
}

export function visibleRunActivity({ activity, run, interaction, streaming, trace }, now = Date.now()) {
  const status = String(run?.status || '');
  const active = ['queued', 'running', 'stopping'].includes(status);
  if (interaction && !active) return null;
  if (status && !['queued', 'running', 'stopping'].includes(status)) return null;
  const executionStart = executionStartedAt(run, now);
  let currentActivity = null;
  if (activity) {
    const observedAt = Number(activity.observedAt);
    if (executionStart === undefined
      || (Number.isFinite(observedAt) && observedAt >= executionStart - 1_000)) currentActivity = activity;
  }
  if (currentActivity && !isPostToolPlanningActivity(currentActivity)) return currentActivity;
  if (!['queued', 'running', 'stopping'].includes(status)) return currentActivity;
  const traceActivity = traceRunActivity(trace, run, executionStart);
  if (traceActivity) return traceActivity;
  if (currentActivity) return currentActivity;
  return {
    id: `run:${run?.id || 'active'}`,
    text: status === 'stopping'
      ? '正在停止任务'
      : status === 'queued'
      ? '任务已提交，等待开始'
      : streaming ? '助手正在生成回复' : '助手正在处理当前步骤',
    detail: status === 'stopping'
      ? '正在核对在途修改，确认不会继续写入后才算停止'
      : status === 'queued'
      ? '任务会在服务端开始，你可以继续输入或离开页面'
      : streaming ? '内容正在实时输出，你可以随时插话' : '正在等待下一项进度，你可以继续输入',
    progress: {},
    sub: [],
    startedAt: executionStart,
  };
}

/* 三级递进的第一级。不进历史，任务结束后折叠成一条正常消息。
   展示当前动作和真实进度，不显示累计运行时长。 */
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
  if (!act) return null;
  const progress = Array.isArray(act.progress)
    ? { current: act.progress[0], total: act.progress[1] }
    : act.progress || {};
  const current = Number(progress.current) || 0;
  const total = Number(progress.total) || 0;
  const rawPercent = Number(progress.percent);
  const percent = Number.isFinite(rawPercent) ? Math.max(0, Math.min(100, rawPercent)) : undefined;
  const detail = act.detail || '正在等待下一项进度，你可以继续输入或停止任务';
  const progressText = total > 0
    ? `${current}/${total} ${progress.unit || '项'}`
    : percent !== undefined ? `${Math.round(percent)}%` : '';
  const sub = Array.isArray(act.sub) ? act.sub : [];
  const activityText = userFacingText(act.text);
  const runStatus = String(run?.status || '');
  const canCancel = ['queued', 'running'].includes(runStatus);
  const stopping = runStatus === 'stopping';

  return (
    <div className="wb-act">
      <button className="row" onClick={actions.toggleActivity} aria-expanded={open} title={activityText}>
        <span className="sp" aria-hidden="true" />
        <span className="tx">
          {activityText}
        </span>
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
          {stopping && <b className="wb-act-stopping">停止中…</b>}
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
