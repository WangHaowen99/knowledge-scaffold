import type { AuthoringInteraction } from '../authoring/types.js';

export type GenerationBatchKind = 'object' | 'knowledge' | 'operation';

export interface GenerationGateFailure {
  phase: string;
  batchKind: GenerationBatchKind;
  batchIndex: number;
  title: string;
  issues: string[];
}

export interface GenerationNotice extends GenerationGateFailure {
  decision: 'expert_skipped';
}

export interface GenerationOptions {
  /** 仅跳过专家本次明确确认的批次；其他批次失败时仍重新询问。 */
  skipFailedPhases?: string[];
  /** 回答生成门禁后继续原任务，避免留下另一个永久运行的 authoring run。 */
  resumeRunId?: string;
}

export interface GenerationContinuation {
  resumeRunId: string;
  skipFailedPhases: string[];
}

export function resolveGenerationContinuation(
  interaction: AuthoringInteraction | null,
): GenerationContinuation {
  if (!interaction) throw new Error('生成门禁待办不存在');
  if (interaction.status !== 'answered') throw new Error('请先回答生成门禁待办');
  if (interaction.type !== 'decision' || interaction.payload.purpose !== 'generation_gate_skip') {
    throw new Error('待办不属于知识生成门禁');
  }
  const phase = String(interaction.payload.phase ?? '');
  if (!/^(?:object|knowledge|operation)_batch_[1-9]\d*$/u.test(phase)) {
    throw new Error('生成门禁阶段不正确');
  }
  const decision = String(interaction.response?.value ?? interaction.response?.decision ?? '');
  if (decision !== 'retry' && decision !== 'skip') {
    throw new Error('生成门禁回答必须是 retry 或 skip');
  }
  return {
    resumeRunId: interaction.runId,
    skipFailedPhases: decision === 'skip' ? [phase] : [],
  };
}

export class GenerationGateDecisionRequired extends Error {
  constructor(public readonly failure: GenerationGateFailure) {
    super(`${failure.title}：${failure.issues.join('；')}`);
    this.name = 'GenerationGateDecisionRequired';
  }
}

export class GenerationWaitingForExpert extends Error {
  constructor(
    public readonly failure: GenerationGateFailure,
    public readonly interaction: AuthoringInteraction,
  ) {
    super(`等待专家确认是否跳过：${failure.title}`);
    this.name = 'GenerationWaitingForExpert';
  }
}
