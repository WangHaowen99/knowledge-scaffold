// 已发布版本的「评测报告 / 优化建议」按钮状态：状态来自建模后端的评测通知索引与对账结果
export const EVALUATION_HINT = '评测完成后才可查看评测报告和优化建议。';
export const POLL_INTERVAL_MS = 5000;
// 轮询只读本地索引；每隔若干次向评测服务对账一次，通知丢失也不会一直停在“进行中”
export const RECONCILE_EVERY = 6;

const enabled = { disabled: false, reason: '' };
const blocked = (reason) => ({ disabled: true, reason });

export function releaseEvaluation(status, releaseId) {
  if (!status) {
    const loading = blocked('正在读取评测状态');
    return { entry: null, report: loading, suggestions: loading };
  }
  if (status.configured === false) {
    const reason = blocked(status.message || '未配置评测服务');
    return { entry: null, report: reason, suggestions: reason };
  }
  if (status.active?.release_id === releaseId) {
    const running = blocked('评测进行中，完成后可查看');
    return { entry: null, report: running, suggestions: running };
  }
  const entry = status.releases?.[releaseId];
  if (!entry) {
    const missing = blocked('该版本尚未完成评测');
    return { entry: null, report: missing, suggestions: missing };
  }
  const artifacts = entry.artifacts || {};
  const report = artifacts.report === 'ready' ? enabled
    : blocked(artifacts.report === 'failed' ? '评测报告生成失败' : '评测报告生成中');
  const suggestions = ['ready', 'failed'].includes(artifacts.suggestions) ? enabled
    : blocked('优化建议生成中');
  return { entry, report, suggestions };
}

export function needsPolling(status) {
  if (!status || status.configured === false) return false;
  if (status.active) return true;
  return Object.values(status.releases || {}).some(({ artifacts = {} }) => (
    artifacts.status === 'generating' || ['pending', 'generating'].includes(artifacts.suggestions)
  ));
}

// 评测优化只针对当前建模项目的版本；首页弹窗没有聊天区，无法发送优化命令
export function optimizationBlock(status, releaseId, canCommand) {
  if (!canCommand) return '请在项目内打开已发布版本后执行评测优化';
  if (status?.current_release_id !== releaseId) {
    return `该版本与当前建模项目版本 ${status?.current_version || '未发布'} 不一致，不能评测优化`;
  }
  return '';
}

// 对账轮次之间的本地读取不带 reachable：沿用上次对账结论，避免告警闪烁
export function mergeStatus(previous, next) {
  return next.reachable == null && previous ? { ...next, reachable: previous.reachable } : next;
}
