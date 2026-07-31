import React from 'react';

const VIEWS = [
  ['cases', '评测用例'],
  ['run', '运行与结果'],
  ['diagnose', '失败诊断'],
  ['optimize', '优化更新'],
];

export function evaluationLifecycleTarget(view) {
  if (view === 'diagnose' || view === 'optimize') {
    return { workspaceTab: 'improve', view, workflowStepId: view };
  }
  return { workspaceTab: 'evaluate', view: view === 'run' ? 'run' : 'cases', workflowStepId: 'evaluate' };
}

export default function EvaluationLifecycleTabs({ activeView = 'cases', onSelect }) {
  return (
    <nav className="pkw-subtabs pkw-evaluation-tabs" role="tablist" aria-label="评测与改进">
      {VIEWS.map(([id, label]) => (
        <button key={id} type="button" role="tab" className={activeView === id ? 'on' : ''}
          aria-selected={activeView === id} onClick={() => onSelect?.(id)}>{label}</button>
      ))}
    </nav>
  );
}
