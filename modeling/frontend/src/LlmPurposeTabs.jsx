import React from 'react';

export const LLM_PURPOSE_HELP = '主模型用于核心建模、资料理解和知识生成；后台任务模型用于流程草稿、评测判分、诊断优化等辅助任务。后台任务模型未配置时自动使用主模型配置。';

export default function LlmPurposeTabs({
  purpose,
  onChange,
  ariaLabel = 'LLM 用途',
  className = 'personal-llm-purpose',
}) {
  return (
    <div className="llm-purpose-row">
      <div className={className} role="tablist" aria-label={ariaLabel}>
        <button type="button" className={purpose === 'main' ? 'on' : ''} onClick={() => onChange('main')}>主模型</button>
        <button type="button" className={purpose === 'task' ? 'on' : ''} onClick={() => onChange('task')}>后台任务模型</button>
      </div>
      {purpose === 'task' && (
        <div className="tag tag-lang cfg-purpose-tag" role="note">{LLM_PURPOSE_HELP}</div>
      )}
    </div>
  );
}
