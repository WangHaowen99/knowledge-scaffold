import type {
  DocumentRecord,
  PackageSpec,
  Project,
  WorkflowNode,
} from '../types.js';
import type { ProjectDataSource } from '../evaluation/data-source-repo.js';
import type { StoredFailureDiagnosis } from '../evaluation/failure-diagnosis-repo.js';
import type { AiConstructionTaskGraphSnapshot } from '../ai-construction/task-graph.js';
import { deriveAiConstructionPhaseStatuses } from './ai-construction-workflow.js';
import type { AuthoringSnapshot } from './types.js';
import type { WorkflowStepProgress } from './workflow-progress.js';
import {
  deriveWorkflowSteps,
  WORKFLOW_STEP_IDS,
  type ReviewedWorkflowStatus,
  type WorkflowFacts,
  type WorkflowStepFact,
  type WorkflowStepId,
} from './workflow-definition.js';

interface CurrentPackage {
  revision: { oid: string };
  packageVersion: string;
  spec: PackageSpec;
}

interface PackageCheck {
  version: string;
  revision?: string;
  findings: Array<{ severity: string }>;
}

interface Testcase {
  id: string;
}

interface EvalRound {
  id: string;
  status: 'running' | 'done' | 'failed';
}

interface EvalResult {
  verdict?: 'pass' | 'partial' | 'fail' | 'error';
}

interface OptimizationSuggestion {
  status: 'pending' | 'candidate' | 'applied' | 'rejected';
  packageOptimizable?: boolean;
  blocking?: boolean;
  roundId?: string;
}

export interface ProjectWorkflowStateInput {
  project: Project;
  documents: DocumentRecord[];
  /** 仅为调用方兼容保留；九步运行时不再读取 wf_nodes。 */
  nodes: WorkflowNode[];
  currentPackage: CurrentPackage | null;
  packageCheck: PackageCheck | null;
  dataSource: ProjectDataSource | null;
  testcases: Testcase[];
  rounds: EvalRound[];
  latestResults: EvalResult[];
  diagnoses: StoredFailureDiagnosis[];
  suggestions: OptimizationSuggestion[];
  authoring: AuthoringSnapshot;
  progress?: WorkflowStepProgress[];
  aiConstruction?: AiConstructionTaskGraphSnapshot | null;
  releaseAvailable?: boolean;
  releaseReady?: boolean;
}

const TERMINAL_STATUSES = new Set<ReviewedWorkflowStatus>(['completed', 'skipped']);

function hasTableBackedKnowledge(spec: PackageSpec | undefined): boolean {
  return Boolean(spec?.objects.some((object) => (
    Boolean(object.sourceTable)
    || object.sourceRefs?.some((source) => source.kind === 'table')
  )));
}

function interactionTypes(authoring: AuthoringSnapshot): Set<string> {
  return new Set(authoring.pendingInteractions.map((interaction) => interaction.type));
}

function currentWorkflowSteps(steps: readonly string[]): WorkflowStepId[] {
  const known = new Set<string>(WORKFLOW_STEP_IDS);
  return steps.filter((step): step is WorkflowStepId => known.has(step));
}

function statusFact(status: ReviewedWorkflowStatus): WorkflowStepFact {
  return status === 'not_started' ? {} : { status };
}

function packageCheckIsCurrent(currentPackage: CurrentPackage, packageCheck: PackageCheck): boolean {
  return packageCheck.version === currentPackage.packageVersion
    && (!packageCheck.revision || packageCheck.revision === currentPackage.revision.oid);
}

/**
 * 直接从任务图和各业务事实生成唯一九步流程，不再经过旧 wf_nodes 状态机。
 */
export function buildProjectWorkflow(input: ProjectWorkflowStateInput) {
  const steps: NonNullable<WorkflowFacts['steps']> = {
    project_setup: { status: 'completed' },
  };
  const enabled = new Set<WorkflowStepId>(currentWorkflowSteps(input.project.workflowProfile.enabledSteps));
  const required = new Set<WorkflowStepId>(currentWorkflowSteps(input.project.workflowProfile.requiredSteps));
  const pending = interactionTypes(input.authoring);
  const canonicalProgress = (input.progress ?? []).filter((item) => (
    WORKFLOW_STEP_IDS.includes(item.stepId)
  ));
  const progress = new Map(canonicalProgress.map((item) => [item.stepId, item]));
  const recordedStatus = (stepId: WorkflowStepId): ReviewedWorkflowStatus | undefined => {
    const item = progress.get(stepId);
    if (!item) return undefined;
    if (item.revision && input.currentPackage?.revision.oid
      && item.revision !== input.currentPackage.revision.oid
      && ['completed', 'waiting_answer', 'waiting_review'].includes(item.status)) {
      return 'stale';
    }
    return item.status;
  };

  const construction = deriveAiConstructionPhaseStatuses(
    input.aiConstruction ?? null,
    input.rounds.length > 0,
  );
  const packageImport = input.aiConstruction?.source === 'package_import';
  const parsedDocuments = input.documents.filter((document) => document.parseStatus === 'parsed');
  if (packageImport) {
    steps.ingest = { status: 'skipped' };
  } else if (!input.documents.length) {
    steps.ingest = { available: true };
  } else if (input.documents.some((document) => document.parseStatus === 'pending')) {
    steps.ingest = { status: 'running' };
  } else if (parsedDocuments.length) {
    steps.ingest = { status: 'completed' };
  } else {
    steps.ingest = { status: 'failed' };
  }

  if (input.aiConstruction) {
    steps.ai_construction = statusFact(construction.aiConstruction);
  } else {
    steps.ai_construction = {
      available: TERMINAL_STATUSES.has(steps.ingest.status ?? 'not_started'),
    };
  }
  const constructionDone = construction.aiConstruction === 'completed';

  const scenarioStatus = pending.has('scenario_review')
    ? 'waiting_review'
    : recordedStatus('scenario');
  steps.scenario = scenarioStatus
    ? { status: scenarioStatus }
    : { available: Boolean(constructionDone && input.currentPackage) };

  if (hasTableBackedKnowledge(input.currentPackage?.spec)) required.add('data_source');
  const dataSourceProgress = recordedStatus('data_source');
  if (dataSourceProgress === 'stale' || dataSourceProgress === 'failed') {
    steps.data_source = { status: dataSourceProgress };
  } else if (input.dataSource?.mode === 'connection') {
    steps.data_source = { status: 'completed' };
  } else if (input.dataSource?.mode === 'none') {
    steps.data_source = { status: 'skipped' };
  } else if (dataSourceProgress) {
    steps.data_source = { status: dataSourceProgress };
  } else {
    steps.data_source = { available: Boolean(constructionDone && input.currentPackage) };
  }

  const latestRound = input.rounds[0];
  const validationPrerequisitesReady = constructionDone
    && TERMINAL_STATUSES.has(steps.scenario.status ?? 'not_started')
    && TERMINAL_STATUSES.has(steps.data_source.status ?? 'not_started');
  if (construction.evaluationRepair) {
    steps.evaluate = statusFact(construction.evaluationRepair);
  } else if (latestRound) {
    steps.evaluate = {
      status: latestRound.status === 'done' ? 'completed' : latestRound.status,
    };
  } else {
    steps.evaluate = {
      available: Boolean(validationPrerequisitesReady && input.currentPackage && input.testcases.length),
    };
  }
  if (input.testcases.length || latestRound) enabled.add('evaluate');

  const evaluationNeedsDiagnosis = Boolean(latestRound && (
    latestRound.status === 'failed'
    || (latestRound.status === 'done'
      && input.latestResults.some((result) => result.verdict !== 'pass'))
  ));
  if (evaluationNeedsDiagnosis) required.add('diagnose');
  const diagnosesAreCurrent = !input.currentPackage || input.diagnoses.every((diagnosis) => (
    diagnosis.basedRevisionOid === input.currentPackage?.revision.oid
  ));
  if (input.diagnoses.length) {
    steps.diagnose = { status: diagnosesAreCurrent ? 'completed' : 'stale' };
  } else if (evaluationNeedsDiagnosis) {
    steps.diagnose = { available: true };
  }

  const pendingSuggestions = input.suggestions.filter((suggestion) => suggestion.status === 'pending');
  const candidateSuggestions = input.suggestions.filter((suggestion) => suggestion.status === 'candidate');
  const eligibleDiagnoses = input.diagnoses.filter((diagnosis) => (
    diagnosis.packageOptimizable && !diagnosis.blocking
  ));
  const pendingPackageChange = input.authoring.pendingInteractions.some((interaction) => (
    interaction.type === 'change_plan'
      && interaction.payload.purpose === 'package_chat'
  ));
  if (pendingPackageChange || pendingSuggestions.length || candidateSuggestions.length) {
    steps.optimize = { status: 'waiting_review' };
  } else if (input.suggestions.some((suggestion) => suggestion.status === 'applied')) {
    steps.optimize = { status: 'completed' };
  } else if (input.suggestions.length
    && input.suggestions.every((suggestion) => suggestion.status === 'rejected')) {
    steps.optimize = { status: 'skipped' };
  } else if (eligibleDiagnoses.length && diagnosesAreCurrent) {
    enabled.add('optimize');
    steps.optimize = { available: true };
  }

  const releaseRequested = input.project.workflowProfile.releaseRequested
    || input.releaseAvailable === true
    || input.releaseReady === true;
  if (input.currentPackage?.spec.status === 'released') {
    steps.release = { status: 'completed' };
  } else if (releaseRequested && input.currentPackage) {
    if (input.packageCheck && !packageCheckIsCurrent(input.currentPackage, input.packageCheck)) {
      steps.release = { status: 'stale' };
    } else if (input.packageCheck?.findings.some((finding) => finding.severity === '错误')) {
      steps.release = { status: 'waiting_review' };
    } else {
      steps.release = { available: true };
    }
  }

  const reviewed = deriveWorkflowSteps({
    steps,
    enabledSteps: [...enabled],
    requiredSteps: [...required],
    releaseRequested,
  }).map((step) => ({ ...step, label: step.name }));

  return {
    profile: input.project.workflowProfile,
    progress: canonicalProgress,
    steps: reviewed,
  };
}
