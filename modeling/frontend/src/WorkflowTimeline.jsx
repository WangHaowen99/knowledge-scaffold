import React from 'react';

const GROUPS = [
  { id: 'prepare', label: '准备' },
  { id: 'model', label: '建模' },
  { id: 'verify', label: '验证' },
  { id: 'improve', label: '改进' },
  { id: 'deliver', label: '交付' },
];

const STATUS_ALIASES = {
  not_started: 'not_started',
  pending: 'not_started',
  locked: 'not_started',
  未开始: 'not_started',
  ready: 'ready',
  available: 'ready',
  可开始: 'ready',
  running: 'running',
  in_progress: 'running',
  进行中: 'running',
  waiting_answer: 'waiting_answer',
  waiting_user: 'waiting_answer',
  待回答: 'waiting_answer',
  waiting_review: 'waiting_review',
  review: 'waiting_review',
  待审阅: 'waiting_review',
  completed: 'completed',
  done: 'completed',
  完成: 'completed',
  skipped: 'skipped',
  已跳过: 'skipped',
  stale: 'stale',
  已过时: 'stale',
  failed: 'failed',
  error: 'failed',
  失败: 'failed',
};

const STATUS_LABELS = {
  not_started: '未开始',
  ready: '可开始',
  running: '进行中',
  waiting_answer: '待回答',
  waiting_review: '待审阅',
  completed: '完成',
  skipped: '已跳过',
  stale: '待确认',
  failed: '待确认',
};

const REQUIREMENT_LABELS = {
  conditional: '条件',
  condition: '条件',
  conditional_required: '条件',
  条件必选: '条件',
  optional: '可选',
  可选: '可选',
  release: '发布',
  publish: '发布',
  publish_required: '发布',
  release_required: '发布',
  正式发布必选: '发布',
};

// 必须与 FALLBACK_STEP_IDS 一一对应：移除「建模资料上传」后这里少了一项 prepare，
// 不同步就会整体错位一格，把 verify 的步骤归到 improve 下面。
const FALLBACK_GROUPS = [
  'prepare',
  'model',
  'verify', 'verify', 'verify',
  'improve', 'improve',
  'deliver',
];

const FALLBACK_STEP_IDS = [
  'project_setup', 'ai_construction',
  'scenario', 'data_source', 'evaluate',
  'diagnose', 'optimize', 'release',
];

const fallbackGroup = (step, visibleIndex) => {
  const definitionIndex = FALLBACK_STEP_IDS.indexOf(step?.id);
  return FALLBACK_GROUPS[definitionIndex >= 0 ? definitionIndex : visibleIndex] || 'deliver';
};

const groupTitleStyle = {
  margin: '14px 2px 7px',
  color: 'var(--muted)',
  fontSize: 11,
  fontWeight: 700,
};

const stepStyle = {
  width: '100%',
  font: 'inherit',
  textAlign: 'left',
};

const titleStyle = {
  flex: '1 1 80px',
  minWidth: 0,
  overflowWrap: 'anywhere',
};

const statusStyle = {
  flex: 'none',
  whiteSpace: 'nowrap',
};

const canonicalStatus = (status) => STATUS_ALIASES[String(status ?? '')] || 'not_started';

export const workflowStatusLabel = (status) => STATUS_LABELS[canonicalStatus(status)];

export const workflowRequirementLabel = (requirement) => (
  REQUIREMENT_LABELS[String(requirement ?? '')] || ''
);

export const canSelectWorkflowStep = (step) => canonicalStatus(step?.status) !== 'not_started';

export function selectWorkflowStep(step, onSelect) {
  if (!canSelectWorkflowStep(step)) return false;
  onSelect?.(step);
  return true;
}

export function groupWorkflowSteps(steps = []) {
  const grouped = Object.fromEntries(GROUPS.map((group) => [group.id, []]));
  const source = (Array.isArray(steps) ? steps : []).filter((step) => step?.linear !== false);
  source.forEach((step, index) => {
    if (!step || !step.id) return;
    const group = grouped[step?.group] ? step.group : fallbackGroup(step, index);
    grouped[group].push({ step, index });
  });
  return grouped;
}

const statusClass = (status) => {
  const value = canonicalStatus(status);
  if (value === 'completed') return 'done';
  if (value === 'running') return 'running';
  if (value === 'stale') return 'waiting_review';
  if (value === 'failed') return 'waiting_review';
  return value;
};

function StepMark({ status, number }) {
  const value = canonicalStatus(status);
  if (value === 'completed') return '✓';
  if (value === 'skipped') return '-';
  if (value === 'stale') return '?';
  if (value === 'failed') return '?';
  if (value === 'running') return <span className="spinner" />;
  if (value === 'waiting_answer' || value === 'waiting_review') return '?';
  return number;
}

/**
 * Props:
 * - steps: [{ id, name/label, status, requirement, focus, group }]
 * - activeId: selected step id
 * - onSelect(step): receives the original step object
 */
export default function WorkflowTimeline({ steps = [], activeId = '', onSelect, compact = false }) {
  const grouped = groupWorkflowSteps(steps);

  return (
    <nav aria-label="建模流程" className={`workflow-timeline${compact ? ' compact' : ''}`}>
      {GROUPS.map((group) => {
        const items = grouped[group.id];
        if (!items.length) return null;
        return (
          <section
            key={group.id}
            aria-label={compact ? group.label : undefined}
            aria-labelledby={compact ? undefined : `workflow-group-${group.id}`}
          >
            {!compact && <div id={`workflow-group-${group.id}`} style={groupTitleStyle}>{group.label}</div>}
            {items.map(({ step, index }) => {
              const selectable = canSelectWorkflowStep(step);
              const active = step.id === activeId;
              const status = canonicalStatus(step.status);
              const label = step.label || step.name || step.id;
              return (
                <button
                  key={`${step.id}-${index}`}
                  type="button"
                  data-step-id={step.id}
                  data-step-status={status}
                  data-tour={`node-${step.focus || step.id}`}
                  className={`tl-node ${statusClass(status)} ${active ? 'active' : ''} ${selectable ? 'clickable' : ''}`.trim()}
                  style={{ ...stepStyle, ...(!selectable ? { opacity: 0.62, cursor: 'not-allowed' } : {}) }}
                  disabled={!selectable}
                  aria-current={active ? 'step' : undefined}
                  aria-label={compact ? `${label} · ${STATUS_LABELS[status]}` : undefined}
                  title={compact ? `${label} · ${STATUS_LABELS[status]}` : undefined}
                  onClick={() => selectWorkflowStep(step, onSelect)}
                >
                  <span className="th">
                    <span className="no"><StepMark status={status} number={index + 1} /></span>
                    {(!compact || active) && (
                      <span className="tt" style={{ ...titleStyle, ...(!selectable ? { color: 'var(--muted)' } : {}) }}>{label}</span>
                    )}
                    {(!compact || active) && <span className="ts" style={statusStyle}>{STATUS_LABELS[status]}</span>}
                  </span>
                </button>
              );
            })}
          </section>
        );
      })}
    </nav>
  );
}
