import { durationLabel, estimatedLabel } from './progressTime.js';

const PHASES = {
  prepare: {
    completedSteps: 0,
    current: () => '检查所选优化建议',
    next: ['模型修改知识包', '校验修改结果', '保存待确认的修改'],
  },
  revision: {
    completedSteps: 1,
    current: (state) => (() => { if (state.protocol === 'targeted_incremental') return state.shardCount
        ? `${(() => { if (state.shardOrdering === 'dependency_serial') return '正在按依赖顺序生成'; return '正在并行生成'; })()} ${state.shardCount} 个目标符号补丁（第 ${state.attempt}/${state.maxAttempts} 次）`
        : `正在生成 ${state.changeSetCount || 1} 个目标资源变更组（第 ${state.attempt}/${state.maxAttempts} 次）`; return `模型正在修改知识包（第 ${state.attempt}/${state.maxAttempts} 次）`; })(),
    next: ['校验修改结果', '保存待确认的修改'],
  },
  validation: {
    completedSteps: 2,
    current: (state) => `正在校验修改结果（第 ${state.attempt}/${state.maxAttempts} 次）`,
    next: ['保存待确认的修改'],
  },
  candidate: {
    completedSteps: 3,
    current: () => '正在保存待确认的修改',
    next: [],
  },
  done: { completedSteps: 4, current: () => '已生成待确认的修改', next: [] },
  failed: { completedSteps: 0, current: () => '优化候选生成失败', next: [] },
};

/** 根据服务端持久化任务计算展示状态；刷新或切页后传入同一 node 即可恢复。 */
export function optimizationProgress(node, now = Date.now()) {
  const state = node?.output;
  if (!state || state.taskType !== 'optimization_apply' || !PHASES[state.phase]) return null;
  const phase = PHASES[state.phase];
  const startedAt = Date.parse(state.startedAt || node.createdAt);
  const phaseStartedAt = Date.parse(state.phaseStartedAt || state.startedAt || node.createdAt);
  const elapsedSeconds = Math.max(0, Math.round((now - startedAt) / 1000));
  const phaseElapsed = Math.max(0, (now - phaseStartedAt) / 1000);
  // 完整规格会随知识资源数增长；线上 30 个左右资源、3 条建议曾耗时约 18 分钟。
  // 初始估算取保守值，避免给用户一个明显偏短的倒计时。
  const revisionSeconds = state.protocol === 'targeted_incremental'
    ? 300
    : Math.min(1800,
      360 + Math.max(0, Number(state.resourceCount) || 0) * 12
        + Math.max(0, Number(state.selectedCount) || 0) * 60);
  const estimateOverdue = state.phase === 'revision' && phaseElapsed >= revisionSeconds;
  let estimatedSeconds = 0;
  if (state.phase === 'prepare') estimatedSeconds = Math.max(5, 10 - phaseElapsed) + revisionSeconds + 15 + 10;
  if (state.phase === 'revision') estimatedSeconds = Math.max(30, revisionSeconds - phaseElapsed) + 15 + 10;
  if (state.phase === 'validation') estimatedSeconds = Math.max(5, 15 - phaseElapsed) + 10;
  if (state.phase === 'candidate') estimatedSeconds = Math.max(3, 10 - phaseElapsed);
  if (estimateOverdue) estimatedSeconds = 0;
  estimatedSeconds = Math.round(estimatedSeconds);
  const completedSteps = state.phase === 'failed' ? Math.min(3, Number(state.completedSteps) || 0) : phase.completedSteps;
  const totalSteps = 4;
  const remainingRetries = Math.max(0, Number(state.maxAttempts) - Number(state.attempt));
  const nextSteps = state.phase === 'validation' && remainingRetries
    ? [`校验未通过时自动修正（最多剩余 ${remainingRetries} 次）`, ...phase.next]
    : phase.next;

  return {
    status: node.status,
    completedSteps,
    totalSteps,
    percent: Math.round((completedSteps / totalSteps) * 100),
    currentStep: phase.current(state),
    nextSteps,
    elapsedSeconds,
    elapsedLabel: durationLabel(elapsedSeconds),
    estimatedSeconds,
    estimatedLabel: estimateOverdue ? '模型响应较慢，暂无法准确估算' : estimatedLabel(estimatedSeconds),
  };
}
