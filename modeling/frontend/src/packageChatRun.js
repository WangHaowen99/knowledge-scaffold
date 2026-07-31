// 与后端 src/routes.ts 的 PACKAGE_CHAT_RUN_PURPOSE 保持一致
export const PACKAGE_CHAT_RUN_PURPOSE = 'package_chat';

/**
 * 建模助手是否正在后台分析。
 * 以服务端运行记录为准而不是本地 busy：刷新页面或换标签页也能恢复该状态。
 */
export function isPackageChatRunning(authoring) {
  return (authoring?.activeRuns ?? []).some((run) => (
    run?.state?.purpose === PACKAGE_CHAT_RUN_PURPOSE
  ));
}
