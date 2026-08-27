import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, FolderOpen } from 'lucide-react';

import PackageWorkspace from './PackageWorkspace.jsx';
import { IngestView, ProjectSetupView } from './PreparationPanels.jsx';
import Tour, { AI_CONSTRUCTION_WORKBENCH_STEPS } from './Tour.jsx';
import WorkflowTimeline, {
  canSelectWorkflowStep,
  workflowStatusLabel,
} from './WorkflowTimeline.jsx';
import { confirmUnsavedNavigation } from './flowchartNavigationGuard.js';

const STATUS_PRIORITY = {
  waiting_answer: 0,
  waiting_review: 1,
  running: 2,
  failed: 3,
  stale: 4,
  ready: 5,
  completed: 6,
  skipped: 7,
};

export function initialAiConstructionStepId(steps = []) {
  const selectable = steps.filter(canSelectWorkflowStep);
  if (!selectable.length) return steps[0]?.id ?? '';
  return selectable.reduce((best, step) => (
    (STATUS_PRIORITY[step.status] ?? 99) < (STATUS_PRIORITY[best.status] ?? 99)
      ? step
      : best
  )).id;
}

export function workspaceNavigationTarget(stepId) {
  if (stepId === 'diagnose' || stepId === 'optimize') {
    return { workflowStepId: 'evaluate', workspaceFocus: stepId };
  }
  return { workflowStepId: stepId, workspaceFocus: '' };
}

export async function selectWorkflowStepAfterRefresh({
  stepId,
  currentStepId = '',
  refresh,
  select,
  fallbackSteps = [],
}) {
  if (stepId === currentStepId) {
    const current = fallbackSteps.find((step) => step.id === stepId);
    if (!current || !canSelectWorkflowStep(current)) return false;
    select(stepId);
    return true;
  }
  const nextSession = await refresh?.();
  const nextSteps = nextSession?.workflow?.steps ?? fallbackSteps;
  const target = nextSteps.find((step) => step.id === stepId);
  if (!target || !canSelectWorkflowStep(target)) return false;
  select(stepId);
  return true;
}

export function CollapsedWorkflowStep({ step, index, total }) {
  if (!step) return null;
  const label = step.label ?? step.name ?? step.id;
  const status = workflowStatusLabel(step.status);
  const position = Number.isInteger(index) && Number.isInteger(total) && total > 0
    ? `${index + 1}/${total}`
    : '';
  return (
    <div className="acw-flow-compact" aria-current="step" title={`${label} · ${status}`}>
      {position && <span className="acw-flow-compact-position">当前 · {position}</span>}
      <strong>{label}</strong>
      <span>{status}</span>
    </div>
  );
}

export default function AiConstructionWorkbench({ projectId, session, onProjectChanged, onUnsavedChange }) {
  const allSteps = session?.workflow?.steps ?? [];
  const steps = allSteps.filter((step) => step?.linear !== false);
  const [selectedStepId, setSelectedStepId] = useState(() => initialAiConstructionStepId(steps));
  const [workspaceFocus, setWorkspaceFocus] = useState('');
  const [collapsed, setCollapsed] = useState(false);
  const [materialsOpen, setMaterialsOpen] = useState(false);
  const [workspaceDirty, setWorkspaceDirty] = useState(false);
  const reportWorkspaceDirty = useCallback((dirty) => {
    setWorkspaceDirty(dirty);
    onUnsavedChange?.(dirty);
  }, [onUnsavedChange]);
  useEffect(() => () => { onUnsavedChange?.(false); }, [onUnsavedChange]);

  useEffect(() => {
    const current = steps.find((step) => step.id === selectedStepId);
    if ((!current || !canSelectWorkflowStep(current)) && !workspaceDirty) {
      setSelectedStepId(initialAiConstructionStepId(steps));
    }
  }, [steps, selectedStepId, workspaceDirty]);

  const selected = useMemo(() => (
    steps.find((step) => step.id === selectedStepId)
    ?? steps.find(canSelectWorkflowStep)
    ?? steps[0]
  ), [steps, selectedStepId]);
  const currentStep = useMemo(() => {
    const currentId = initialAiConstructionStepId(steps);
    return steps.find((step) => step.id === currentId) ?? selected;
  }, [steps, selected]);

  const focus = workspaceFocus || selected?.focus || selected?.id || 'ai_construction';
  const title = selected?.label ?? selected?.name ?? 'AI辅助构建';
  const navigateFromWorkspace = (stepId) => {
    if (!confirmUnsavedNavigation(workspaceDirty)) return;
    const target = workspaceNavigationTarget(stepId);
    void selectWorkflowStepAfterRefresh({
      stepId: target.workflowStepId,
      currentStepId: selectedStepId,
      refresh: onProjectChanged,
      select: (nextStepId) => {
        setMaterialsOpen(false);
        reportWorkspaceDirty(false);
        setSelectedStepId(nextStepId);
        setWorkspaceFocus(target.workspaceFocus);
      },
      fallbackSteps: steps,
    });
  };

  return (
    <div className={`ai-construction-workbench${collapsed ? ' collapsed' : ''}`}>
      <Tour scene="ai-construction-workbench" steps={AI_CONSTRUCTION_WORKBENCH_STEPS} />
      <aside className="acw-flow">
        <header className="acw-flow-head">
          {!collapsed && <strong>建模流程</strong>}
          <button
            type="button"
            className="acw-collapse"
            aria-label={collapsed ? '展开建模流程' : '收起建模流程'}
            title={collapsed ? '展开建模流程' : '收起建模流程'}
            onClick={() => setCollapsed((value) => !value)}
          >
            {collapsed ? '›' : '‹'}
          </button>
        </header>
        <CollapsedWorkflowStep
          step={currentStep}
          index={steps.findIndex((step) => step.id === currentStep?.id)}
          total={steps.length}
        />
        <div className="acw-flow-body" data-tour="timeline">
          <WorkflowTimeline
            steps={steps}
            activeId={selected?.id}
            compact={collapsed}
            onSelect={(step) => {
              if (step.id === selectedStepId || confirmUnsavedNavigation(workspaceDirty)) {
                if (step.id !== selectedStepId) reportWorkspaceDirty(false);
                setMaterialsOpen(false);
                setWorkspaceFocus('');
                setSelectedStepId(step.id);
              }
            }}
          />
        </div>
      </aside>
      <main className="acw-stage">
        <header className="acw-stage-head">
          <strong>{materialsOpen ? '建模资料' : title}</strong>
          <button type="button" className="btn acw-materials-button"
            title={materialsOpen ? '返回当前建模步骤' : '查看、下载或补充建模资料'}
            onClick={() => {
              if (!materialsOpen && !confirmUnsavedNavigation(workspaceDirty)) return;
              if (!materialsOpen) reportWorkspaceDirty(false);
              setMaterialsOpen((value) => !value);
            }}>
            {materialsOpen
              ? <><ArrowLeft size={14} aria-hidden="true" />返回当前步骤</>
              : <><FolderOpen size={14} aria-hidden="true" />建模资料 {session.documents?.length ?? 0}</>}
          </button>
        </header>
        <div className={`acw-stage-body focus-${focus}`}>
          {materialsOpen ? (
            <IngestView
              project={session.project}
              documents={session.documents ?? []}
              onChanged={onProjectChanged}
            />
          ) : focus === 'project_setup' ? (
            <ProjectSetupView project={session.project} onChanged={onProjectChanged} />
          ) : focus === 'ingest' ? (
            <IngestView
              project={session.project}
              documents={session.documents ?? []}
              onChanged={onProjectChanged}
            />
          ) : (
            <PackageWorkspace
              projectId={projectId}
              initialSession={session}
              embedded
              focus={focus}
              onNavigate={navigateFromWorkspace}
              onUnsavedChange={reportWorkspaceDirty}
            />
          )}
        </div>
      </main>
    </div>
  );
}
