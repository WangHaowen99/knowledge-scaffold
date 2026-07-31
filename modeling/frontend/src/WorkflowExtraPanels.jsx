import React, { useState } from 'react';

import { startScenario } from './authoring-api.js';

function StepMessage({ error, children }) {
  if (error) return <div className="lerr">{error}</div>;
  return children ? <div className="step-message">{children}</div> : null;
}

export function ScenarioPanel({ projectId, step, progress, onChanged, onOpenCollaboration }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const scenarios = Array.isArray(progress?.output?.scenarios) ? progress.output.scenarios : [];
  const start = async () => {
    setBusy(true); setError('');
    try {
      await startScenario(projectId);
      await onChanged?.();
      onOpenCollaboration?.();
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="workflow-extra">
      <div className="workflow-extra-actions">
        <button type="button" className="btn btn-p" disabled={busy} onClick={start}>
          {busy ? '准备中…' : scenarios.length ? '重新推演' : '开始推演'}
        </button>
        {step?.status === 'waiting_review' && (
          <button type="button" className="btn" onClick={onOpenCollaboration}>去确认</button>
        )}
      </div>
      <StepMessage error={error}>
        {step?.status === 'completed' ? '已通过' : step?.status === 'failed' ? '需调整' : ''}
      </StepMessage>
      {scenarios.length > 0 && (
        <div className="scenario-list">
          {scenarios.map((scenario, index) => (
            <div className="scenario-row" key={`${scenario.name}-${index}`}>
              <b>{scenario.name}</b><span>{scenario.input}</span><span>{scenario.expected}</span>
              {Array.isArray(scenario.checks) && <em>{scenario.checks.length} 类推演</em>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
