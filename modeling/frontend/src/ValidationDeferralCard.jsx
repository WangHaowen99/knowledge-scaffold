import React, { useState } from 'react';

export default function ValidationDeferralCard({ errorCount = 0, busy = false, onConfirm, onCancel }) {
  const [reason, setReason] = useState('');
  const valid = reason.trim().length >= 4;
  return (
    <section className="validation-deferral-card" aria-label="暂缓知识包错误修复">
      <header>
        <div><span>验证策略</span><strong>暂缓知识包错误修复</strong></div>
        <em>{errorCount} 个阻断错误</em>
      </header>
      <div className="validation-deferral-warning">
        当前修订将保留 {errorCount} 个阻断错误并进入验证和改进；正式发布前仍必须消除这些错误。
      </div>
      <label>
        <span>说明暂缓原因</span>
        <textarea rows={3} maxLength={2000} value={reason}
          placeholder="例如：先跑通真实场景评测，再依据失败证据统一修复"
          onChange={(event) => setReason(event.target.value)} />
      </label>
      <footer>
        <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>
        <button type="button" className="btn btn-p" disabled={busy || !valid}
          onClick={() => onConfirm?.(reason.trim())}>
          {busy ? '正在暂缓…' : '确认暂缓并继续'}
        </button>
      </footer>
    </section>
  );
}
