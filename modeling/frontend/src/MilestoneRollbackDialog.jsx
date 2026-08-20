import React, { useState } from 'react';

function etaText(eta) {
  const low = Number(eta?.minMinutes ?? 0);
  const high = Number(eta?.maxMinutes ?? 0);
  if (!high) return '重新执行时长将在任务开始后更新';
  return low === high ? `预计重新执行约 ${high} 分钟` : `预计重新执行 ${low}–${high} 分钟`;
}

export default function MilestoneRollbackDialog({ preview, busy = false, onConfirm, onCancel }) {
  const [reason, setReason] = useState('');
  if (!preview) return null;
  return (
    <div className="milestone-rollback-backdrop" role="presentation">
      <section className="milestone-rollback-dialog" role="dialog" aria-modal="true"
        aria-label="阶段回退影响预览">
        <header>
          <div><span>阶段回退预览</span><strong>回退到“{preview.targetMilestone?.title}”</strong></div>
          <em>{etaText(preview.eta)}</em>
        </header>
        {!preview.allowed && <div className="task-adjustment-blocked" role="alert">
          {preview.reason || '当前不能回退到这个阶段'}
        </div>}
        <div className="milestone-rollback-columns">
          <div>
            <strong>会保留</strong>
            <ul>{(preview.preservedOutputs ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
          </div>
          <div>
            <strong>需要重新确认或生成</strong>
            <ul>{(preview.invalidatedOutputs ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
          </div>
        </div>
        {preview.restoreRevisionOid && <p className="milestone-rollback-revision">
          系统会创建新的恢复修订；当前版本和之后的历史版本不会被删除。
        </p>}
        {preview.allowed && <label className="task-adjustment-reason">
          <span>回退原因（必填）</span>
          <textarea rows={3} maxLength={500} value={reason}
            placeholder="说明为什么要回退，便于团队审计和后续复盘"
            onChange={(event) => setReason(event.target.value)} />
        </label>}
        <footer>
          <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>
          {preview.allowed && <button type="button" className="btn btn-p"
            disabled={busy || !reason.trim()} onClick={() => onConfirm?.(reason.trim())}>
            {busy ? '正在回退…' : '确认回退并重新处理'}
          </button>}
        </footer>
      </section>
    </div>
  );
}
