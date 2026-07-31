import React from 'react';

export default function TaskProgressCard({ progress }) {
  if (!progress) return null;
  const completed = progress.completedSteps ?? progress.completedUnits;
  const total = progress.totalSteps ?? progress.totalUnits;
  const countLabel = progress.countLabel
    ?? (Number.isFinite(completed) && Number.isFinite(total) ? `${completed}/${total} 步` : '处理中');

  return (
    <section className="generation-progress" aria-live="polite">
      <div className="generation-progress-head">
        <div>
          <div className="generation-progress-eyebrow">当前步骤</div>
          <div className="generation-progress-current">
            <span className="spinner" />{progress.currentStep}
          </div>
        </div>
        <div className="generation-progress-eta">
          <span>预计剩余</span>
          <b>{progress.estimatedLabel}</b>
        </div>
      </div>
      <div
        className={`generation-progress-bar${progress.indeterminate ? ' indeterminate' : ''}`}
        role="progressbar"
        aria-label={progress.indeterminate ? '任务正在处理' : `已完成 ${completed} / ${total}`}
        {...(!progress.indeterminate && Number.isFinite(progress.percent) ? {
          'aria-valuemin': 0,
          'aria-valuemax': 100,
          'aria-valuenow': progress.percent,
        } : {})}
      >
        <span style={progress.indeterminate ? undefined : { width: `${progress.percent}%` }} />
      </div>
      <div className="generation-progress-count">{countLabel} · 已用 {progress.elapsedLabel}</div>
      <div className="generation-progress-next-title">后续步骤</div>
      {progress.nextSteps.length ? (
        <ol className="generation-progress-next">
          {progress.nextSteps.map((step) => <li key={step}>{step}</li>)}
        </ol>
      ) : (
        <div className="generation-progress-finishing">正在收尾</div>
      )}
    </section>
  );
}
