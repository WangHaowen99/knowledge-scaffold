import { durationLabel, estimatedLabel } from './progressTime.js';

const SETUP_PHASES = ['workspace', 'database', 'package', 'data_source'];
const SETUP_SECONDS = {
  workspace: 15,
  database: 10,
  package: 30,
  data_source: 15,
};
const PHASE_LABELS = {
  workspace: '准备评测工作空间',
  database: '准备评测数据环境',
  package: '导入并激活知识包',
  data_source: '绑定评测数据源',
};
const DEFAULT_CASE_SECONDS = 75;

function parsed(value, fallback) {
  const result = Date.parse(value || '');
  return Number.isFinite(result) ? result : fallback;
}

function boundedCaseSeconds(seconds) {
  return Math.max(15, Math.min(300, seconds));
}

function historyCaseSeconds(rounds, currentId) {
  const samples = rounds
    .filter((round) => round.id !== currentId && round.finishedAt && round.progressDone > 0)
    .map((round) => (
      (Date.parse(round.finishedAt) - Date.parse(round.createdAt)) / 1000 / round.progressDone
    ))
    .filter((seconds) => Number.isFinite(seconds) && seconds > 0)
    .map(boundedCaseSeconds)
    .sort((a, b) => a - b);
  if (!samples.length) return undefined;
  const middle = Math.floor(samples.length / 2);
  return samples.length % 2 ? samples[middle] : (samples[middle - 1] + samples[middle]) / 2;
}

function caseEstimate(round, history, now, phaseStartedAt, currentItemStartedAt) {
  const historic = historyCaseSeconds(history, round.id);
  const itemElapsed = Math.max(0, (now - currentItemStartedAt) / 1000);
  let current;
  if (round.progressDone > 0) {
    const completedWindow = Math.max(0, (currentItemStartedAt - phaseStartedAt) / 1000);
    current = boundedCaseSeconds(completedWindow / round.progressDone);
  }
  const seconds = current !== undefined && historic !== undefined
    ? ((current * 2) + historic) / 3
    : current ?? historic ?? DEFAULT_CASE_SECONDS;
  return { seconds: Math.round(boundedCaseSeconds(seconds)), itemElapsed };
}

function setupRemaining(phase, phaseElapsed) {
  const index = SETUP_PHASES.indexOf(phase);
  if (index < 0) return 0;
  return SETUP_PHASES.slice(index).reduce((total, item, offset) => (
    total + (offset === 0
      ? Math.max(0, SETUP_SECONDS[item] - phaseElapsed)
      : SETUP_SECONDS[item])
  ), 0);
}

export function evaluationProgress(round, history = [], now = Date.now()) {
  if (!round) return null;
  const totalCases = Math.max(0, Number(round.progressTotal) || 0);
  const doneCases = Math.min(totalCases, Math.max(0, Number(round.progressDone) || 0));
  const totalUnits = SETUP_PHASES.length + totalCases;
  const startedAt = parsed(round.createdAt, now);
  const phaseStartedAt = parsed(round.phaseStartedAt, startedAt);
  const currentItemStartedAt = parsed(round.currentItemStartedAt, phaseStartedAt);
  const elapsedSeconds = Math.max(0, Math.round((now - startedAt) / 1000));
  const phaseElapsed = Math.max(0, (now - phaseStartedAt) / 1000);
  const phase = round.phase || (round.status === 'running' ? 'workspace' : round.status);
  const setupIndex = SETUP_PHASES.indexOf(phase);
  const completedUnits = phase === 'done'
    ? totalUnits
    : phase === 'cases'
      ? SETUP_PHASES.length + doneCases
      : Math.max(0, setupIndex);
  const percent = totalUnits ? Math.round((completedUnits / totalUnits) * 100) : 100;

  if (phase === 'done') {
    return {
      status: round.status, completedUnits, totalUnits, percent: 100,
      currentStep: '评测已完成', nextSteps: [], elapsedSeconds,
      countLabel: `${totalCases}/${totalCases} 条用例`,
      elapsedLabel: durationLabel(elapsedSeconds), estimatedSeconds: 0, estimatedLabel: '已完成',
    };
  }
  if (phase === 'failed') {
    return {
      status: round.status, completedUnits, totalUnits, percent,
      currentStep: '评测运行失败', nextSteps: [], elapsedSeconds,
      countLabel: `${doneCases}/${totalCases} 条用例`,
      elapsedLabel: durationLabel(elapsedSeconds), estimatedSeconds: 0, estimatedLabel: '已停止',
    };
  }

  const estimate = caseEstimate(round, history, now, phaseStartedAt, currentItemStartedAt);
  let estimatedSeconds;
  let estimateOverdue = false;
  let currentStep;
  let nextSteps;
  if (phase === 'cases') {
    const remainingAfterCurrent = Math.max(0, totalCases - doneCases - 1);
    estimateOverdue = doneCases < totalCases && estimate.itemElapsed >= estimate.seconds * 2;
    estimatedSeconds = Math.max(0, estimate.seconds - estimate.itemElapsed)
      + remainingAfterCurrent * estimate.seconds;
    currentStep = doneCases < totalCases
      ? `运行第 ${doneCases + 1}/${totalCases} 条评测用例`
      : '汇总评测结果';
    nextSteps = doneCases < totalCases
      ? ['完成当前用例', ...(remainingAfterCurrent ? [`运行剩余 ${remainingAfterCurrent} 条用例`] : []), '汇总评测结果']
      : ['汇总评测结果'];
  } else {
    estimatedSeconds = setupRemaining(phase, phaseElapsed) + totalCases * estimate.seconds;
    currentStep = PHASE_LABELS[phase] || '准备评测';
    nextSteps = [
      ...SETUP_PHASES.slice(setupIndex + 1).map((item) => PHASE_LABELS[item]),
      `运行 ${totalCases} 条评测用例`,
    ];
  }
  estimatedSeconds = Math.max(0, Math.round(estimatedSeconds));

  return {
    status: round.status,
    completedUnits,
    totalUnits,
    percent,
    currentStep,
    nextSteps,
    countLabel: phase === 'cases'
      ? `${doneCases}/${totalCases} 条用例`
      : `准备阶段 ${setupIndex + 1}/${SETUP_PHASES.length}`,
    elapsedSeconds,
    elapsedLabel: durationLabel(elapsedSeconds),
    estimatedSeconds: estimateOverdue ? 0 : estimatedSeconds,
    estimatedLabel: estimateOverdue
      ? '当前用例响应较慢，暂无法准确估算'
      : estimatedLabel(estimatedSeconds),
  };
}
