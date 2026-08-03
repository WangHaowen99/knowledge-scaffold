import { durationLabel, estimatedLabel } from './progressTime.js';

const OPERATIONS = {
  diagnose: {
    seconds: 600,
    currentStep: '读取失败结果、执行轨迹和独立证据',
    nextSteps: ['区分数据、运行、用例与知识包问题', '保存失败诊断'],
  },
  propose: {
    seconds: 180,
    currentStep: '分析诊断证据并生成优化建议',
    nextSteps: ['保存并展示优化建议'],
  },
  apply: {
    seconds: 60,
    currentStep: '正在把所选优化建议整理成待确认的修改',
    nextSteps: ['模型修改知识包', '校验修改结果', '保存待确认的修改'],
  },
};

export function optimizationOperationProgress(task, now = Date.now()) {
  const operation = task?.operation || task?.kind;
  const config = OPERATIONS[operation];
  if (!config) return null;
  const startedAt = Date.parse(task.startedAt || '');
  const elapsedSeconds = Number.isFinite(startedAt)
    ? Math.max(0, Math.round((now - startedAt) / 1000))
    : 0;
  const overdue = elapsedSeconds >= config.seconds;
  const remaining = overdue ? 0 : Math.max(1, config.seconds - elapsedSeconds);
  return {
    status: 'running',
    indeterminate: true,
    currentStep: config.currentStep,
    nextSteps: config.nextSteps,
    elapsedSeconds,
    elapsedLabel: durationLabel(elapsedSeconds),
    estimatedSeconds: remaining,
    estimatedLabel: overdue ? '模型响应较慢，暂无法准确估算' : estimatedLabel(remaining),
  };
}
