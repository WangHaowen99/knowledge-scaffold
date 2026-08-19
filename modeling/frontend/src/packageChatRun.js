// 与后端 src/routes.ts 的 PACKAGE_CHAT_RUN_PURPOSE 保持一致
export const PACKAGE_CHAT_RUN_PURPOSE = 'package_chat';

const PACKAGE_CHAT_RUNNING_STATUSES = new Set(['queued', 'running']);
export const WAITING_INTERACTION_RECOVERY_MS = 30_000;

/**
 * 建模助手是否正在后台分析。
 * 以服务端运行记录为准而不是本地 busy：刷新页面或换标签页也能恢复该状态。
 */
export function isPackageChatRunning(authoring) {
  return (authoring?.activeRuns ?? []).some((run) => (
    run?.state?.purpose === PACKAGE_CHAT_RUN_PURPOSE
    && PACKAGE_CHAT_RUNNING_STATUSES.has(run?.status)
  ));
}

/** 后端已经等待回答、但当前快照还没带回待办卡时的恢复依据。 */
export function waitingPackageChatRun(authoring) {
  return (authoring?.activeRuns ?? []).find((run) => (
    run?.state?.purpose === PACKAGE_CHAT_RUN_PURPOSE
    && run?.status === 'waiting'
  )) ?? null;
}

export function waitingInteractionRecoveryDelay(run, now = Date.now()) {
  const updatedAt = Date.parse(String(run?.updatedAt ?? run?.createdAt ?? ''));
  if (!Number.isFinite(updatedAt)) return WAITING_INTERACTION_RECOVERY_MS;
  return Math.max(0, WAITING_INTERACTION_RECOVERY_MS - (now - updatedAt));
}

export function waitingInteractionTitle(run) {
  return /访谈/u.test(String(run?.intent ?? ''))
    ? '访谈题卡加载失败'
    : '待办卡片加载失败';
}
