import React from 'react';

function elapsedLabel(seconds) {
  if (!Number.isFinite(seconds)) return '';
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
}

export default function RunObservabilityPanel({
  job,
  integrated = false,
  stalledMessage = '',
  cancelling = false,
  onRefresh,
  onCancel,
}) {
  if (!job && !stalledMessage && !onCancel) return null;
  return (
    <div className={`pchat-job-observability${integrated ? ' is-integrated' : ''}`}>
      {job && <>
        {job.elapsedSeconds != null && <span>已运行：{elapsedLabel(job.elapsedSeconds)}</span>}
        {job.eta?.minMinutes != null && job.eta?.maxMinutes != null && (
          <span>预计剩余：{job.eta.minMinutes}–{job.eta.maxMinutes} 分钟</span>
        )}
        {job.stale && <strong role="alert">任务长时间没有更新，可能已中断，请刷新后重试</strong>}
      </>}
      {stalledMessage && !job?.stale && <span className="pchat-job-observability__hint">{stalledMessage}</span>}
      {(job?.stale && onRefresh) || onCancel ? (
        <div className="pchat-job-observability__actions">
          {job?.stale && onRefresh && (
            <button type="button" className="pchat-refresh-job" onClick={onRefresh}>刷新任务状态</button>
          )}
          {onCancel && (
            <button type="button" className="pchat-cancel" disabled={cancelling} onClick={onCancel}>
              {cancelling ? '正在停止…' : '取消'}
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}
