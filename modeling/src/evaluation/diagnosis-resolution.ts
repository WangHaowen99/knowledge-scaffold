import type { DiagnosisResolutionAction } from './failure-diagnosis-repo.js';

export type DiagnosisNextStep =
  | 'review_data_source'
  | 'rerun_evaluation'
  | 'generate_optimization_suggestions';

export interface DiagnosisResolutionPlan {
  nextSteps: DiagnosisNextStep[];
}

export function diagnosisResolutionPlan(action: DiagnosisResolutionAction): DiagnosisResolutionPlan {
  if (action === 'data_updated') {
    return {
      nextSteps: ['review_data_source', 'rerun_evaluation'],
    };
  }
  if (action === 'confirm_rule' || action === 'clarify_semantics') {
    return {
      nextSteps: ['generate_optimization_suggestions', 'rerun_evaluation'],
    };
  }
  return {
    nextSteps: ['rerun_evaluation'],
  };
}
