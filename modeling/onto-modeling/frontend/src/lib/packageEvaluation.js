// 已发布版本的「报告 / 优化建议」链接状态：状态来自建模后端的评测通知索引与对账结果
export const EVALUATION_HINT = '打开优化建议执行评测优化，修改并发布新版本后状态自动变为“已处理”。';
export const POLL_INTERVAL_MS = 5000;
// 轮询只读本地索引；每隔若干次向评测服务对账一次，通知丢失也不会一直停在“进行中”
export const RECONCILE_EVERY = 6;

const enabled = { disabled: false, reason: '' };
const blocked = (reason) => ({ disabled: true, reason });

// 同一版本的两种来源：评测轮次在 releases，消费反馈轮次在 feedback（评测服务的包负责人确认后才有）
export function releaseFeedback(status, releaseId) {
  return releaseEvaluation(status && status.configured !== false
    ? { ...status, active: null, releases: status.feedback || {} } : status, releaseId);
}

export function releaseEvaluation(status, releaseId) {
  if (!status) {
    const loading = blocked('正在读取评测状态');
    return { entry: null, idle: '读取中', report: loading, suggestions: loading };
  }
  if (status.configured === false) {
    const reason = blocked(status.message || '未配置评测服务');
    return { entry: null, idle: '评测服务未配置', report: reason, suggestions: reason };
  }
  if (status.active?.release_id === releaseId) {
    const running = blocked('评测进行中，完成后可查看');
    return { entry: null, idle: '评测中', report: running, suggestions: running };
  }
  const entry = status.releases?.[releaseId];
  if (!entry) {
    const missing = blocked('该版本尚未完成评测');
    return { entry: null, idle: '未评测', report: missing, suggestions: missing };
  }
  const artifacts = entry.artifacts || {};
  const report = artifacts.report === 'ready' ? enabled
    : blocked(artifacts.report === 'failed' ? '评测报告生成失败' : '评测报告生成中');
  const suggestions = ['ready', 'failed'].includes(artifacts.suggestions) ? enabled
    : blocked('优化建议生成中');
  return { entry, report, suggestions };
}

const generating = ({ artifacts = {} }) => artifacts.status === 'generating' || ['pending', 'generating'].includes(artifacts.suggestions);

export function needsPolling(status) {
  if (!status || status.configured === false) return false;
  if (status.active) return true;
  return [...Object.values(status.releases || {}), ...Object.values(status.feedback || {})].some(generating);
}

// 项目还没有任何消费反馈轮次时，已发布版本不显示「用户反馈」一列
export function hasFeedback(status) {
  return Object.keys(status?.feedback || {}).length > 0;
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

// 建模对这一轮优化建议的处理结果，来自评测服务记录的 optimization；还没有记录时只区分产物是否在生成
const PROCESSING = {
  awaiting_review: ['待确认', 'default', '等待包负责人在评测服务确认'],
  notified: ['待处理', 'gold', '打开优化建议执行评测优化，修改并发布新版本后自动变为已处理'],
  modeling_done: ['已处理', 'green', '已按这一轮优化建议发布新版本'],
  modeling_failed: ['处理失败', 'red', '这一轮优化建议处理失败；修改并发布新版本后仍会自动变为已处理'],
  dismissed: ['已驳回', 'default', '包负责人已驳回，无需处理'],
};
export function processingState(entry) {
  const optimization = entry?.optimization;
  const known = optimization && PROCESSING[optimization.status];
  if (!known) return entry && !optimization && generating(entry) ? { label: '生成中', color: 'processing', hint: '报告和优化建议生成中' } : null;
  const result = optimization.result || {};
  const label = optimization.status === 'modeling_done' && result.version ? `已处理 → ${result.version}` : known[0];
  return { label, color: known[1], hint: result.note || optimization.reason || known[2] };
}

// 只有当前版本、已交给建模且尚未处理成功的轮次，可以回写“处理失败”
export function canReportFailure(status, releaseId, entry) {
  return status?.current_release_id === releaseId && ['notified', 'modeling_failed'].includes(entry?.optimization?.status);
}

// 已发布版本顶部只提示还需要留意的回写：已成功的回写看版本行的“已处理”标签
export function writebackNotice(status) {
  const w = status?.writeback;
  if (!w || !w.mine) return null;
  if (w.status === 'done') return null;
  if (w.status === 'pending') return { type: 'warning', text: `新版本 ${w.version} 的处理结果未能回写评测服务（${w.message}），下次打开已发布版本时自动重试` };
  if (w.status === 'refused') return { type: 'warning', text: `评测服务未接受新版本 ${w.version} 的处理结果：${w.message}` };
  if (status.current_release_id === w.base_release_id)
    return { type: 'info', text: `已对版本 ${w.base_version} 执行评测优化，修改并发布新版本后自动变为“已处理”` };
  return null;
}
