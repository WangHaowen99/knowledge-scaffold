export const WORKFLOW_STEP_IDS = [
  'project_setup',
  'ai_construction',
  'scenario',
  'data_source',
  'evaluate',
  'diagnose',
  'optimize',
  'release',
] as const;

export type WorkflowStepId = typeof WORKFLOW_STEP_IDS[number];
export type WorkflowGroup = 'prepare' | 'model' | 'verify' | 'improve' | 'deliver';
export type WorkflowRequirement = 'required' | 'conditional' | 'optional' | 'release_required';
export type WorkbenchFocus = WorkflowStepId;

export type ReviewedWorkflowStatus =
  | 'not_started'
  | 'ready'
  | 'running'
  | 'waiting_answer'
  | 'waiting_review'
  | 'completed'
  | 'skipped'
  | 'stale'
  | 'failed';

export type ObservedWorkflowStatus = Exclude<ReviewedWorkflowStatus, 'not_started' | 'ready'>;

export interface WorkflowStepDefinition {
  id: WorkflowStepId;
  name: string;
  group: WorkflowGroup;
  requirement: WorkflowRequirement;
  focus: WorkbenchFocus;
  capability: string;
  trigger?: string;
  dependsOn?: readonly WorkflowStepId[];
  /** false 表示能力状态仍保留，但不属于线性建模时间线。 */
  linear?: boolean;
}

export interface WorkflowStepFact {
  /** 当前修订已具备运行该步骤的条件。 */
  available?: boolean;
  /** 外部事实可以显式给出 ready；未提供时由依赖关系推导。 */
  status?: ReviewedWorkflowStatus;
}

export interface WorkflowFacts {
  steps?: Partial<Record<WorkflowStepId, WorkflowStepFact>>;
  enabledSteps?: readonly WorkflowStepId[];
  requiredSteps?: readonly WorkflowStepId[];
  releaseRequested?: boolean;
}

export interface ReviewedWorkflowStep extends WorkflowStepDefinition {
  applicable: boolean;
  status: ReviewedWorkflowStatus;
}

export interface WorkflowProfile {
  releaseRequested: boolean;
  enabledSteps: readonly WorkflowStepId[];
  requiredSteps: readonly WorkflowStepId[];
  defaultSkippedSteps: readonly WorkflowStepId[];
  afterFailedEvaluation: readonly WorkflowStepId[];
}

export const WORKFLOW_STEPS = [
  {
    id: 'project_setup', name: '项目创建', group: 'prepare', requirement: 'required',
    focus: 'project_setup', capability: 'project_setup',
  },
    {
    id: 'ai_construction', name: 'AI辅助构建', group: 'model', requirement: 'required',
    focus: 'ai_construction', capability: 'ai_construction', dependsOn: ['project_setup'],
  },
  {
    id: 'scenario', name: '场景推演', group: 'verify', requirement: 'required',
    focus: 'scenario', capability: 'scenario', dependsOn: ['ai_construction'],
  },
  {
    id: 'data_source', name: '数据源绑定', group: 'verify', requirement: 'conditional',
    focus: 'data_source', capability: 'data_source', trigger: '需要查询业务数据时必须完成',
    dependsOn: ['ai_construction'],
  },
  {
    id: 'evaluate', name: '评测', group: 'verify', requirement: 'release_required',
    focus: 'evaluate', capability: 'evaluate', trigger: '正式发布前必须通过',
    dependsOn: ['scenario', 'data_source'],
  },
  {
    id: 'diagnose', name: '失败诊断', group: 'improve', requirement: 'conditional',
    focus: 'diagnose', capability: 'diagnose', trigger: '评测未通过或运行出错时启用', linear: false,
  },
  {
    id: 'optimize', name: '优化更新', group: 'improve', requirement: 'optional',
    focus: 'optimize', capability: 'optimize', trigger: '需要修正问题或提升效果时启用', linear: false,
  },
  {
    id: 'release', name: '发布交付', group: 'deliver', requirement: 'release_required',
    focus: 'release', capability: 'release', trigger: '需要对外交付时启用',
  },
] as const satisfies readonly WorkflowStepDefinition[];

/** 企业对公营销项目的建议开关；诊断和优化只在评测失败后启用。 */
export const CORPORATE_MARKETING_PROFILE = {
  releaseRequested: true,
  enabledSteps: [],
  requiredSteps: ['scenario', 'data_source'],
  defaultSkippedSteps: [],
  afterFailedEvaluation: ['diagnose', 'optimize'],
} as const satisfies WorkflowProfile;

function isApplicable(
  definition: WorkflowStepDefinition,
  fact: WorkflowStepFact | undefined,
  enabled: ReadonlySet<WorkflowStepId>,
  required: ReadonlySet<WorkflowStepId>,
  releaseRequested: boolean,
): boolean {
  if (definition.requirement === 'required') return true;
  const hasStarted = fact?.status !== undefined && !['not_started', 'skipped'].includes(fact.status);
  if (definition.requirement === 'release_required') {
    return releaseRequested || enabled.has(definition.id) || fact?.available === true || hasStarted;
  }
  if (definition.requirement === 'conditional') {
    return required.has(definition.id) || enabled.has(definition.id)
      || fact?.available === true || hasStarted;
  }
  return enabled.has(definition.id) || fact?.available === true || hasStarted;
}

function satisfiesDependency(step: ReviewedWorkflowStep | undefined): boolean {
  if (!step || !step.applicable) return true;
  return step.status === 'completed' || step.status === 'skipped';
}

/**
 * 根据九步运行时的事实和显式依赖生成状态。未提供的事实不会被猜测为已完成。
 */
export function deriveWorkflowSteps(facts: WorkflowFacts = {}): ReviewedWorkflowStep[] {
  const enabled = new Set(facts.enabledSteps ?? []);
  const required = new Set(facts.requiredSteps ?? []);
  const reviewed = new Map<WorkflowStepId, ReviewedWorkflowStep>();

  for (const definition of WORKFLOW_STEPS) {
    const workflowDefinition: WorkflowStepDefinition = definition;
    const fact = facts.steps?.[definition.id];
    const applicable = isApplicable(
      definition,
      fact,
      enabled,
      required,
      facts.releaseRequested === true,
    );
    const dependenciesReady = (workflowDefinition.dependsOn ?? [])
      .every((stepId) => satisfiesDependency(reviewed.get(stepId)));
    const status = fact?.status
      ?? (applicable && fact?.available === true && dependenciesReady ? 'ready' : 'not_started');
    reviewed.set(definition.id, { ...definition, applicable, status });
  }

  return [...reviewed.values()];
}
