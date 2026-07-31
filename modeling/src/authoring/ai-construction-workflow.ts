import type { AiConstructionTaskGraphSnapshot } from '../ai-construction/task-graph.js';
import type { ReviewedWorkflowStatus } from './workflow-definition.js';

export interface AiConstructionPhaseStatuses {
  aiConstruction: ReviewedWorkflowStatus;
  /** 评测后的修缮任务仍由任务图执行，但归属评测阶段。 */
  evaluationRepair?: ReviewedWorkflowStatus;
}

function graphStatus(snapshot: AiConstructionTaskGraphSnapshot | null): ReviewedWorkflowStatus {
  if (!snapshot) return 'not_started';
  if (snapshot.status === 'completed') return 'completed';
  if (snapshot.status === 'superseded') return 'stale';
  const task = snapshot.tasks.find((item) => item.id === snapshot.progress.currentTaskId);
  if (task?.status === 'failed' || snapshot.progress.waitingFor === 'task_failed') return 'failed';
  if (task?.status === 'stale') return 'stale';
  if (task?.status === 'running') return 'running';
  if (task?.status === 'waiting_confirmation'
    || snapshot.progress.waitingFor === 'milestone_confirmation') {
    return 'waiting_review';
  }
  if (task?.status === 'waiting_user' || snapshot.progress.waitingFor === 'user_input') {
    return 'waiting_answer';
  }
  return 'ready';
}

/**
 * 将任务图状态归入九步流程。评测开始后的修缮任务不会重新点亮建模阶段。
 */
export function deriveAiConstructionPhaseStatuses(
  snapshot: AiConstructionTaskGraphSnapshot | null,
  evaluationStarted = false,
): AiConstructionPhaseStatuses {
  const status = graphStatus(snapshot);
  const currentTask = snapshot?.tasks.find((item) => item.id === snapshot.progress.currentTaskId);
  const evaluationRepairActive = evaluationStarted
    && ['package_repair', 'evaluation_repair', 'package_validation'].includes(currentTask?.kind ?? '')
    && !['completed', 'skipped', 'cancelled'].includes(currentTask?.status ?? '');
  return evaluationRepairActive
    ? { aiConstruction: 'completed', evaluationRepair: status }
    : { aiConstruction: status };
}
