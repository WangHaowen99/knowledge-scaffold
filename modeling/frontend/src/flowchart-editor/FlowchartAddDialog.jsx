import React, { useEffect, useState } from 'react';
import { X } from 'lucide-react';

export default function FlowchartAddDialog({
  open,
  candidates = [],
  modelingGoal,
  busy = false,
  error = '',
  onClose,
  onAddCandidate,
  onAddAi,
}) {
  const [instruction, setInstruction] = useState('');
  useEffect(() => {
    if (!open) setInstruction('');
  }, [open]);
  if (!open) return null;
  return (
    <div className="flowchart-add-dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose?.();
    }}>
      <section className="flowchart-add-dialog" role="dialog" aria-modal="true" aria-labelledby="flowchart-add-title">
        <header>
          <div>
            <span>围绕建模目标</span>
            <h3 id="flowchart-add-title">继续添加流程图</h3>
          </div>
          <button type="button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></button>
        </header>
        {modelingGoal?.problem && (
          <div className="flowchart-add-dialog__goal">
            <strong>当前目标</strong><span>{modelingGoal.problem}</span>
          </div>
        )}
        {candidates.length > 0 && (
          <div className="flowchart-add-dialog__section">
            <strong>资料分析推荐流程</strong>
            <div className="flowchart-add-dialog__candidates">
              {candidates.map((candidate, index) => (
                <button key={candidate.candidateId || `${candidate.title}:${index}`} type="button" disabled={busy}
                  data-candidate-id={candidate.candidateId || undefined}
                  onClick={() => onAddCandidate?.(candidate)}>
                  <span>{candidate.title}</span>
                  <small>{candidate.rationale || candidate.trigger || '根据资料分析形成'}</small>
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="flowchart-add-dialog__section">
          <label htmlFor="flowchart-ai-instruction">让 AI 补充一个新流程</label>
          <textarea id="flowchart-ai-instruction" rows={3} value={instruction}
            placeholder="可补充流程范围、触发场景或关注重点；留空时由 AI 根据当前目标推荐"
            disabled={busy} onChange={(event) => setInstruction(event.target.value)} />
        </div>
        {error && <div className="flowchart-add-dialog__error" role="alert">{error}</div>}
        <footer>
          <button type="button" disabled={busy} onClick={onClose}>取消</button>
          <button type="button" className="is-primary" disabled={busy}
            onClick={() => onAddAi?.(instruction.trim())}>
            {busy ? '正在生成…' : 'AI 生成新流程'}
          </button>
        </footer>
      </section>
    </div>
  );
}
