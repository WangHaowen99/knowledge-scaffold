import React, { useState } from 'react';

const ACTION_LABEL = { skip: '跳过', resume: '恢复', defer: '暂缓', redo: '重做' };

function etaDeltaText(delta) {
  if (!delta) return '预计时长基本不变';
  const min = Number(delta.minMinutes ?? 0);
  const max = Number(delta.maxMinutes ?? 0);
  if (min === 0 && max === 0) return '预计时长基本不变';
  const direction = max < 0 ? '减少' : '增加';
  const low = Math.abs(min);
  const high = Math.abs(max);
  return low === high ? `预计${direction} ${high} 分钟` : `预计${direction} ${low}–${high} 分钟`;
}

export default function TaskAdjustmentPreviewCard({ preview, busy = false, onConfirm, onCancel }) {
  const [reason, setReason] = useState('');
  if (!preview) return null;
  return (
    <section className="task-adjustment-preview" aria-label="任务调整影响预览">
      <header>
        <div><span>任务调整预览</span><strong>{ACTION_LABEL[preview.action] ?? preview.action}“{preview.task?.title}”</strong></div>
        <em>{etaDeltaText(preview.etaDelta)}</em>
      </header>
      {!preview.allowed && <div className="task-adjustment-blocked">{preview.reason || '当前任务不能执行该调整'}</div>}
      <div className="task-adjustment-impact">
        <strong>将受影响的任务</strong>
        <ul>{(preview.affectedTasks ?? []).map((task) => <li key={task.id}>{task.title}</li>)}</ul>
        {(preview.affectedMilestones ?? []).length > 0 && <p>影响里程碑：{preview.affectedMilestones.join('、')}</p>}
      </div>
      {preview.allowed && <label className="task-adjustment-reason">
        <span>调整原因</span>
        <textarea rows={2} value={reason} maxLength={500} placeholder="说明为什么需要这次调整，便于后续审计"
          onChange={(event) => setReason(event.target.value)} />
      </label>}
      <footer>
        <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>
        {preview.allowed && <button type="button" className="btn btn-p" disabled={busy || !reason.trim()}
          onClick={() => onConfirm?.(reason.trim())}>{busy ? '应用中…' : `确认${ACTION_LABEL[preview.action] ?? '调整'}`}</button>}
      </footer>
    </section>
  );
}
