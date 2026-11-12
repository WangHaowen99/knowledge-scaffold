// 已发布版本的「报告 / 优化建议」链接状态：状态来自建模后端的评测通知索引与对账结果（ES-02，ADR-0092；照 develop 的 lib/packageEvaluation.js）
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
  // 同一版本正在求解验证时，数据评测的结果仍可查看
  if (status.active?.release_id === releaseId && (status.active.run_type || 'data') === 'data') {
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
  return ['releases', 'feedback', 'solver_releases'].some((section) => Object.values(status[section] || {}).some(generating));
}

// 项目还没有任何消费反馈轮次时，已发布版本不显示「用户反馈」一列
export function hasFeedback(status) {
  return Object.keys(status?.feedback || {}).length > 0;
}

// ---- 求解验证：发布后在独立评测中对固定版本执行，结果与数据评测分开索引 ----

export const SOLVER_VALIDATION_STATES = {
  unverified: { label: '未验证', color: 'warning' },
  running: { label: '验证中', color: 'processing' },
  passed: { label: '已通过', color: 'success' },
  failed: { label: '未通过', color: 'error' },
};

// 版本的求解验证状态；发布时未启用求解契约、也没有验证记录的版本返回 null（不显示）
export function releaseSolverValidation(status, releaseId) {
  if (!status) return null;
  const tracked = Object.prototype.hasOwnProperty.call(status.solver_revisions || {}, releaseId);
  const running = status.active?.run_type === 'solver' && status.active.release_id === releaseId;
  const entry = status.solver_releases?.[releaseId] || null;
  if (running) return { state: 'running', entry, reason: '求解验证进行中' };
  if (!entry) return tracked ? { state: 'unverified', entry: null, reason: '该版本尚未完成求解验证' } : null;
  const summary = entry.solver_summary;
  const passed = entry.status === 'completed' && summary?.total > 0 && summary.failed === 0;
  if (passed) return { state: 'passed', entry, reason: `${summary.total} 条用例全部通过` };
  const reason = summary?.first_failure_message || summary?.first_failure_code || '求解验证未正常结束';
  return { state: 'failed', entry, reason: `最近一次求解验证未通过：${reason}` };
}

// 中断的轮次没有 finished_at，以报告生成时间排序（与后端评测索引同一口径）
const validatedAt = (entry) => entry.finished_at || entry.artifacts?.generated_at || 0;

// 当前求解契约修订对应的发布与验证：unpublished 尚未发布；published 已发布未通过验证；passed 已通过。
// 以该修订最近一次求解验证为准：同修订的较新版本验证失败时不算已通过，验证进行中也不算（发布确认后端同一规则）
export function currentRevisionValidation(status, revision) {
  const revisions = status?.solver_revisions || {};
  const releases = Object.keys(revisions).filter((id) => revision && revisions[id] === revision);
  if (!releases.length) return { state: 'unpublished' };
  const running = releases.some((id) => releaseSolverValidation(status, id)?.state === 'running');
  const latest = releases
    .filter((id) => status.solver_releases?.[id])
    .sort((a, b) => validatedAt(status.solver_releases[b]) - validatedAt(status.solver_releases[a]))[0];
  if (!running && latest && releaseSolverValidation(status, latest)?.state === 'passed') {
    return { state: 'passed', releaseId: latest, version: status.solver_releases[latest].version };
  }
  const current = releases.includes(status.current_release_id) ? status.current_release_id : releases[0];
  return { state: 'published', releaseId: current, validation: releaseSolverValidation(status, current) };
}

// 求解页面「去评测」的目标版本：当前修订已发布时评测其对应版本；有草稿或尚未发布时只能评测最近发布的
// 版本，它不含当前修改（stale），点击前需提示；从未发布过时返回 null
export function solverEvaluationTarget(status, revision, dirty) {
  const current = currentRevisionValidation(status, revision);
  if (!dirty && current.releaseId) {
    return { release: { ontology_id: status.ontology_id, release_id: current.releaseId }, stale: false };
  }
  if (!status?.current_release_id) return null;
  return {
    release: { ontology_id: status.ontology_id, release_id: status.current_release_id },
    version: status.current_version,
    stale: true,
  };
}

// 发布入口与求解面板不在同一组件分支：发布成功后经窗口事件通知已挂载的面板重读评测状态
export const PACKAGE_PUBLISHED = 'onto-package-published';

export function notifyPackagePublished(projectId) {
  window.dispatchEvent(new CustomEvent(PACKAGE_PUBLISHED, { detail: { projectId } }));
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

// 发布成功后的提示（develop 的 usePackagePublish）：求解包带「去评测」，评测优化之后发布的新版本带回写结果。

// 评测优化交给反馈演进插件（ADR-0092）：插件没启用时「评测优化」置灰并说明
export const EVOLUTION_DISABLED = '评测优化由反馈演进插件处理，项目没有启用它';
