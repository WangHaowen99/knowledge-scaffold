import React from 'react';
import { FileUp, Plus, Sparkles } from 'lucide-react';

export default function AddFlowchartCard({
  onRecommend, onCreateBlank, onImportMermaid, disabled = false, disabledReason = '',
}) {
  return (
    <section className="flowchart-add-card" aria-label="继续添加流程图">
      <div className="flowchart-add-card__mark" aria-hidden="true"><Plus size={18} /></div>
      <strong>继续添加流程图</strong>
      <p>让 AI 补充、导入 Mermaid，或从空白画布开始。</p>
      {disabled && disabledReason && <small className="flowchart-add-card__reason">{disabledReason}</small>}
      <div className="flowchart-add-card__actions">
        <button type="button" className="flowchart-add-card__primary" title={disabledReason || undefined}
          aria-label="AI 推荐新流程" disabled={disabled} onClick={onRecommend}>
          <Sparkles size={14} aria-hidden="true" /> AI 推荐新流程
        </button>
        <button type="button" className="flowchart-add-card__secondary" title={disabledReason || undefined}
          aria-label="导入 Mermaid 流程" disabled={disabled} onClick={onImportMermaid}>
          <FileUp size={14} aria-hidden="true" /> 导入 Mermaid
        </button>
        <button type="button" className="flowchart-add-card__secondary" title={disabledReason || undefined}
          aria-label="新建空白流程" disabled={disabled} onClick={onCreateBlank}>
          <Plus size={14} aria-hidden="true" /> 新建空白流程
        </button>
      </div>
    </section>
  );
}
