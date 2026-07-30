import type { DiagnosisResolutionAction } from './failure-diagnosis-repo.js';
import type { NodeType } from '../types.js';

export type DiagnosisWorkflowStepId = Extract<NodeType, 'data_source' | 'evaluate' | 'optimize'>;

export interface DiagnosisResolutionPlan {
  nextSteps: DiagnosisWorkflowStepId[];
  staleSteps: DiagnosisWorkflowStepId[];
}

export function diagnosisResolutionPlan(action: DiagnosisResolutionAction): DiagnosisResolutionPlan {
  if (action === 'data_updated') {
    return {
      nextSteps: ['data_source', 'evaluate'],
      staleSteps: ['data_source', 'evaluate', 'optimize'],
    };
  }
  if (action === 'confirm_rule' || action === 'clarify_semantics') {
    return {
      nextSteps: ['optimize', 'evaluate'],
      staleSteps: ['evaluate', 'optimize'],
    };
  }
  return {
    nextSteps: ['evaluate'],
    staleSteps: [],
  };
}
